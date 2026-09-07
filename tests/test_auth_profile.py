from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.datastructures import Headers, UploadFile
from starlette.responses import Response

from app.core.exceptions import AuthenticationError
from app.models.auth import UpdateProfileRequest
from app.routers import auth as auth_router
from app.services import auth_service
from app.services.aws_s3_service import AWSS3Service


USER_ID = UUID('11111111-1111-4111-8111-111111111111')
OTHER_USER_ID = UUID('22222222-2222-4222-8222-222222222222')
AVATAR_URL = f'https://assets.example/user-profiles/{USER_ID}/avatar/avatar.png'


class AsyncConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def profile_row(**overrides):
    row = {
        'id': USER_ID,
        'email': 'person@example.com',
        'name': 'Person Name',
        'user_name': 'person',
        'description': 'Personal description',
        'logo_avatar': AVATAR_URL,
        'preferred_locale': 'en',
        'pos_catalog_layout_override': None,
        'created_at': datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


def session_row(**overrides):
    row = {
        **profile_row(),
        'user_id': USER_ID,
        'user_created_at': datetime(2026, 1, 1, tzinfo=timezone.utc),
        'tenant_id': None,
        'expires_at': datetime.now(timezone.utc) + timedelta(hours=1),
        'created_at': datetime.now(timezone.utc),
        'ip_address': None,
        'login_method': 'magic_link',
    }
    row.update(overrides)
    return row


def upload_file(content: bytes, content_type: str, filename: str = 'avatar.png') -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename=filename,
        headers=Headers({'content-type': content_type}),
    )


def test_update_profile_request_normalizes_and_validates_fields():
    payload = UpdateProfileRequest(
        name='  Person Name  ',
        description='  A short description  ',
        preferred_locale='pt',
        pos_catalog_layout_override='LIST',
    )

    assert payload.name == 'Person Name'
    assert payload.description == 'A short description'
    assert payload.preferred_locale == 'pt'
    assert payload.pos_catalog_layout_override == 'list'

    with pytest.raises(ValidationError):
        UpdateProfileRequest(name='   ')
    with pytest.raises(ValidationError):
        UpdateProfileRequest(preferred_locale='it')
    with pytest.raises(ValidationError):
        UpdateProfileRequest(pos_catalog_layout_override='masonry')


def test_update_profile_request_tracks_explicit_null_locale():
    payload = UpdateProfileRequest.model_validate({
        'preferred_locale': None,
        'pos_catalog_layout_override': None,
        'user_id': str(OTHER_USER_ID),
        'logo_avatar': 'https://attacker.example/avatar.png',
    })

    assert 'preferred_locale' in payload.model_fields_set
    assert payload.preferred_locale is None
    assert 'pos_catalog_layout_override' in payload.model_fields_set
    assert payload.pos_catalog_layout_override is None
    assert 'user_id' not in payload.model_dump()
    assert 'logo_avatar' not in payload.model_dump()


@pytest.mark.asyncio
async def test_session_returns_personal_profile_without_active_tenant(monkeypatch):
    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value=session_row(
            pos_catalog_layout_override='list',
        )),
        execute=AsyncMock(),
    )
    monkeypatch.setattr(auth_service, 'get_session_token', AsyncMock(return_value='session-token'))
    monkeypatch.setattr(
        auth_service,
        'get_db_connection',
        lambda: AsyncConnectionContext(connection),
    )
    monkeypatch.setattr(
        'app.core.security.touch_session_activity',
        AsyncMock(),
    )

    result = await auth_service.get_session_data(MagicMock(), Response())
    payload = result.model_dump(by_alias=True)

    assert payload['currentTenant'] is None
    assert payload['user']['user_name'] == 'person'
    assert payload['user']['description'] == 'Personal description'
    assert payload['user']['logo_avatar'] == AVATAR_URL
    assert payload['user']['preferred_locale'] == 'en'
    assert payload['user']['pos_catalog_layout_override'] == 'list'


@pytest.mark.asyncio
async def test_update_profile_uses_session_user_and_can_clear_nullable_fields(monkeypatch):
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value=profile_row(
        description=None,
        preferred_locale=None,
        pos_catalog_layout_override=None,
    )))
    monkeypatch.setattr(
        auth_service,
        'require_valid_session',
        lambda request: SimpleNamespace(user_id=USER_ID),
    )
    monkeypatch.setattr(
        auth_service,
        'get_db_connection',
        lambda: AsyncConnectionContext(connection),
    )

    result = await auth_service.update_profile(
        MagicMock(),
        description=None,
        preferred_locale=None,
        pos_catalog_layout_override=None,
        fields_set={'description', 'preferred_locale', 'pos_catalog_layout_override'},
    )

    query, description, locale, layout, owner_id = connection.fetchrow.await_args.args
    assert 'description = $1' in query
    assert 'preferred_locale = $2' in query
    assert 'pos_catalog_layout_override = $3' in query
    assert 'WHERE id = $4' in query
    assert (description, locale, layout, owner_id) == (None, None, None, USER_ID)
    assert owner_id != OTHER_USER_ID
    assert result.user.preferred_locale is None
    assert result.user.pos_catalog_layout_override is None


@pytest.mark.asyncio
async def test_update_profile_sets_pos_catalog_layout_override(monkeypatch):
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value=profile_row(
        pos_catalog_layout_override='grid',
    )))
    monkeypatch.setattr(
        auth_service,
        'require_valid_session',
        lambda request: SimpleNamespace(user_id=USER_ID),
    )
    monkeypatch.setattr(
        auth_service,
        'get_db_connection',
        lambda: AsyncConnectionContext(connection),
    )

    result = await auth_service.update_profile(
        MagicMock(),
        pos_catalog_layout_override='grid',
        fields_set={'pos_catalog_layout_override'},
    )

    query, layout, owner_id = connection.fetchrow.await_args.args
    assert 'pos_catalog_layout_override = $1' in query
    assert (layout, owner_id) == ('grid', USER_ID)
    assert result.user.pos_catalog_layout_override == 'grid'


@pytest.mark.asyncio
async def test_avatar_endpoint_rejects_spoofed_content_and_oversized_files():
    with pytest.raises(HTTPException) as spoofed:
        await auth_router.upload_profile_avatar_endpoint(
            MagicMock(),
            upload_file(b'not a png', 'image/png'),
        )
    assert spoofed.value.status_code == 400

    oversized_png = b'\x89PNG\r\n\x1a\n' + b'x' * (auth_router.MAX_AVATAR_SIZE + 1)
    with pytest.raises(HTTPException) as oversized:
        await auth_router.upload_profile_avatar_endpoint(
            MagicMock(),
            upload_file(oversized_png, 'image/png'),
        )
    assert oversized.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('content_type', 'image_bytes'),
    [
        ('image/jpeg', b'\xff\xd8\xffimage-data'),
        ('image/png', b'\x89PNG\r\n\x1a\nimage-data'),
        ('image/webp', b'RIFF\x04\x00\x00\x00WEBPimage-data'),
    ],
)
async def test_avatar_endpoint_accepts_supported_image_without_tenant_dependency(
    monkeypatch,
    content_type,
    image_bytes,
):
    upload = AsyncMock(return_value={
        'success': True,
        'url': AVATAR_URL,
        'logo_avatar': AVATAR_URL,
    })
    monkeypatch.setattr(auth_router, 'upload_profile_avatar', upload)
    request = MagicMock()

    result = await auth_router.upload_profile_avatar_endpoint(
        request,
        upload_file(image_bytes, content_type),
    )

    assert result['logo_avatar'] == AVATAR_URL
    upload.assert_awaited_once_with(
        request=request,
        file_bytes=image_bytes,
        content_type=content_type,
    )


@pytest.mark.asyncio
async def test_r2_avatar_key_is_generated_from_session_user_and_mime():
    service = object.__new__(AWSS3Service)
    service.upload_public_asset = AsyncMock(return_value=AVATAR_URL)

    result = await service.upload_user_avatar(b'png', str(USER_ID), 'image/png')

    assert result == AVATAR_URL
    kwargs = service.upload_public_asset.await_args.kwargs
    assert kwargs['s3_key'].startswith(f'user-profiles/{USER_ID}/avatar/')
    assert kwargs['s3_key'].endswith('.png')
    assert str(OTHER_USER_ID) not in kwargs['s3_key']
    assert kwargs['metadata']['user_id'] == str(USER_ID)


@pytest.mark.asyncio
async def test_avatar_upload_persists_server_url_for_session_user(monkeypatch):
    storage = SimpleNamespace(upload_user_avatar=AsyncMock(return_value=AVATAR_URL))
    connection = SimpleNamespace(fetchrow=AsyncMock(return_value={'id': USER_ID}))
    monkeypatch.setattr(auth_service, 'AWSS3Service', lambda: storage)
    monkeypatch.setattr(
        auth_service,
        'require_valid_session',
        lambda request: SimpleNamespace(user_id=USER_ID),
    )
    monkeypatch.setattr(
        auth_service,
        'get_db_connection',
        lambda: AsyncConnectionContext(connection),
    )

    result = await auth_service.upload_profile_avatar(MagicMock(), b'png', 'image/png')

    storage.upload_user_avatar.assert_awaited_once_with(
        file_bytes=b'png',
        user_id=str(USER_ID),
        content_type='image/png',
    )
    query, url, owner_id = connection.fetchrow.await_args.args
    assert 'WHERE id = $2' in query
    assert (url, owner_id) == (AVATAR_URL, USER_ID)
    assert result.logo_avatar == AVATAR_URL


@pytest.mark.asyncio
async def test_avatar_upload_requires_authenticated_session(monkeypatch):
    def reject_session(request):
        raise AuthenticationError('Session expired')

    monkeypatch.setattr(auth_service, 'require_valid_session', reject_session)
    monkeypatch.setattr(
        auth_service,
        'AWSS3Service',
        lambda: pytest.fail('storage must not initialize without a valid session'),
    )

    with pytest.raises(AuthenticationError):
        await auth_service.upload_profile_avatar(MagicMock(), b'png', 'image/png')


@pytest.mark.asyncio
async def test_avatar_upload_failure_does_not_write_profile(monkeypatch):
    storage = SimpleNamespace(upload_user_avatar=AsyncMock(return_value=None))
    monkeypatch.setattr(auth_service, 'AWSS3Service', lambda: storage)
    monkeypatch.setattr(
        auth_service,
        'require_valid_session',
        lambda request: SimpleNamespace(user_id=USER_ID),
    )
    monkeypatch.setattr(auth_service.settings, 'r2_public_url', 'https://assets.example')
    database_called = False

    def fail_if_database_is_called():
        nonlocal database_called
        database_called = True
        raise AssertionError('database must not be called after upload failure')

    monkeypatch.setattr(auth_service, 'get_db_connection', fail_if_database_is_called)

    with pytest.raises(HTTPException) as error:
        await auth_service.upload_profile_avatar(MagicMock(), b'png', 'image/png')

    assert error.value.status_code == 500
    assert database_called is False
