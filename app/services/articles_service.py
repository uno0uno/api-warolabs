from typing import Optional
from uuid import UUID
from fastapi import Request, Response
from app.database import get_db_connection
from app.core.country_locale import normalize_article_country_code
from app.core.exceptions import APIError
from app.models.article import (
    Article, ArticleSummary, ArticlesListResponse, ArticleDetailResponse, AuthorInfo
)
import logging

logger = logging.getLogger(__name__)

_SUMMARY_COLUMNS = """
                    a.id,
                    a.title,
                    a.slug,
                    a.description,
                    a.thumbnail,
                    a.cover,
                    a.tags,
                    a.pillar,
                    COALESCE(a.views, 0) as views,
                    a.published,
                    a.created_at,
                    a.updated_at,
                    a.lang,
                    a.country,
                    a.country_code,
                    p.name as author_name,
                    p.logo_avatar as author_avatar
"""


def _row_get(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _article_summary_from_row(row) -> ArticleSummary:
    country_code = _row_get(row, "country_code")
    if country_code:
        country_code = str(country_code).strip().upper() or None
    else:
        country_code = normalize_article_country_code(_row_get(row, "country"))
    return ArticleSummary(
        id=row["id"],
        title=row["title"],
        slug=row["slug"],
        description=row["description"],
        thumbnail=row["thumbnail"],
        cover=row["cover"],
        tags=row["tags"],
        pillar=row["pillar"],
        views=row["views"],
        published=row["published"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        author_name=row["author_name"],
        author_avatar=row["author_avatar"],
        lang=_row_get(row, "lang"),
        country_code=country_code,
    )


async def get_articles_list(
    request: Request,
    response: Response,
    tenant_id: UUID,
    page: int = 1,
    limit: int = 10,
    search: Optional[str] = None,
    tag: Optional[str] = None,
    pillar: Optional[str] = None
) -> ArticlesListResponse:
    """
    Get published articles list for a tenant (public endpoint for blog).
    Returns only published and active articles.
    """
    try:
        offset = (page - 1) * limit

        async with get_db_connection() as conn:
            # Build query with filters
            where_clauses = [
                "a.tenant_id = $1",
                "a.published = true",
                "a.is_active = true"
            ]
            params = [tenant_id]
            param_count = 1

            if search:
                param_count += 1
                where_clauses.append(f"(a.title ILIKE ${param_count} OR a.description ILIKE ${param_count})")
                params.append(f"%{search}%")

            if tag:
                param_count += 1
                where_clauses.append(f"a.tags ILIKE ${param_count}")
                params.append(f"%{tag}%")

            if pillar:
                param_count += 1
                where_clauses.append(f"a.pillar = ${param_count}")
                params.append(pillar)

            where_sql = " AND ".join(where_clauses)

            # Count total
            count_query = f"""
                SELECT COUNT(*) as total
                FROM articles a
                WHERE {where_sql}
            """
            total_result = await conn.fetchrow(count_query, *params)
            total = total_result['total']

            # Get articles with author info
            param_count += 1
            limit_param = param_count
            param_count += 1
            offset_param = param_count

            query = f"""
                SELECT
                    {_SUMMARY_COLUMNS}
                FROM articles a
                LEFT JOIN profile p ON a.author = p.id
                WHERE {where_sql}
                ORDER BY a.created_at DESC
                LIMIT ${limit_param} OFFSET ${offset_param}
            """
            params.extend([limit, offset])

            rows = await conn.fetch(query, *params)

            articles = [_article_summary_from_row(row) for row in rows]

            return ArticlesListResponse(
                success=True,
                total=total,
                data=articles
            )

    except Exception as e:
        logger.error(f"Error getting articles list: {e}")
        raise APIError(
            message=f"Error fetching articles: {str(e)}",
            status_code=500,
            details={"error_code": "ARTICLES_LIST_ERROR"}
        )


async def get_article_by_slug(
    request: Request,
    tenant_id: UUID,
    slug: str,
    increment_views: bool = True
) -> ArticleDetailResponse:
    """
    Get a single article by slug (public endpoint for blog post page).
    Only returns published and active articles.
    Optionally increments view count.
    """
    try:
        async with get_db_connection() as conn:
            query = """
                SELECT
                    a.id,
                    a.title,
                    a.slug,
                    a.description,
                    a.content,
                    a.meta_title,
                    a.meta_descripcion,
                    a.thumbnail,
                    a.cover,
                    a.tags,
                    a.pillar,
                    COALESCE(a.views, 0) as views,
                    a.published,
                    a.draft,
                    a.is_active,
                    a.author,
                    a.id_profile,
                    a.tenant_id,
                    a.lang,
                    a.planet,
                    a.country,
                    a.country_code,
                    a.city,
                    a.created_at,
                    a.updated_at,
                    p.name as author_name,
                    p.logo_avatar as author_avatar,
                    p.user_name as author_user_name,
                    p.description as author_description,
                    p.city as author_city,
                    p.country as author_country,
                    t.name as tenant_name
                FROM articles a
                LEFT JOIN profile p ON a.author = p.id
                LEFT JOIN tenants t ON a.tenant_id = t.id
                WHERE a.tenant_id = $1
                  AND a.slug = $2
                  AND a.published = true
                  AND a.is_active = true
            """

            row = await conn.fetchrow(query, tenant_id, slug)

            if not row:
                raise APIError(
                    message=f"Article with slug '{slug}' not found",
                    status_code=404,
                    details={"error_code": "ARTICLE_NOT_FOUND"}
                )

            # Increment view count if requested
            if increment_views:
                await conn.execute(
                    "UPDATE articles SET views = COALESCE(views, 0) + 1 WHERE id = $1",
                    row['id']
                )
                from app.routers.trail import schedule_crawler_page_view
                schedule_crawler_page_view(request, slug)

            # Build author info
            author_info = AuthorInfo(
                name=row['author_name'],
                avatar=row['author_avatar'],
                user_name=row['author_user_name'],
                description=row['author_description'],
                city=row['author_city'],
                country=row['author_country']
            )

            article = Article(
                id=row['id'],
                title=row['title'],
                slug=row['slug'],
                description=row['description'],
                content=row['content'],
                meta_title=row['meta_title'],
                meta_descripcion=row['meta_descripcion'],
                thumbnail=row['thumbnail'],
                cover=row['cover'],
                tags=row['tags'],
                pillar=row['pillar'],
                views=row['views'] + (1 if increment_views else 0),
                published=row['published'],
                draft=row['draft'],
                is_active=row['is_active'],
                author=row['author'],
                id_profile=row['id_profile'],
                tenant_id=row['tenant_id'],
                lang=row['lang'],
                planet=row['planet'],
                country=row['country'],
                country_code=(
                    str(row['country_code']).strip().upper()
                    if row['country_code']
                    else normalize_article_country_code(row['country'])
                ),
                city=row['city'],
                created_at=row['created_at'],
                updated_at=row['updated_at'],
                author_name=row['author_name'],
                author_info=author_info,
                tenant_name=row['tenant_name']
            )

            return ArticleDetailResponse(
                success=True,
                data=article
            )

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Error getting article by slug: {e}")
        raise APIError(
            message=f"Error fetching article: {str(e)}",
            status_code=500,
            details={"error_code": "ARTICLE_FETCH_ERROR"}
        )


async def get_related_articles(
    tenant_id: UUID,
    current_article_id: int,
    tags: str,
    limit: int = 3
) -> ArticlesListResponse:
    """
    Get related articles based on tags.
    Excludes the current article.
    """
    try:
        async with get_db_connection() as conn:
            # Split tags and create search pattern
            tag_list = [tag.strip() for tag in tags.split(',') if tag.strip()]

            if not tag_list:
                return ArticlesListResponse(success=True, total=0, data=[])

            # Build OR conditions for tags
            tag_conditions = " OR ".join([f"a.tags ILIKE '%{tag}%'" for tag in tag_list])

            query = f"""
                SELECT
                    {_SUMMARY_COLUMNS}
                FROM articles a
                LEFT JOIN profile p ON a.author = p.id
                WHERE a.tenant_id = $1
                  AND a.id != $2
                  AND a.published = true
                  AND a.is_active = true
                  AND ({tag_conditions})
                ORDER BY a.created_at DESC
                LIMIT $3
            """

            rows = await conn.fetch(query, tenant_id, current_article_id, limit)

            articles = [_article_summary_from_row(row) for row in rows]

            return ArticlesListResponse(
                success=True,
                total=len(articles),
                data=articles
            )

    except Exception as e:
        logger.error(f"Error getting related articles: {e}")
        raise APIError(
            message=f"Error fetching related articles: {str(e)}",
            status_code=500,
            details={"error_code": "RELATED_ARTICLES_ERROR"}
        )
