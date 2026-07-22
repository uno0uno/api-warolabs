from fastapi import APIRouter, Request, Response, Query
from typing import Optional
from app.core.middleware import get_tenant_context
from app.services.articles_service import (
    get_articles_list,
    get_article_by_slug,
    get_related_articles
)
from app.models.article import (
    ArticlesListResponse,
    ArticleDetailResponse
)

router = APIRouter()


@router.get("", response_model=ArticlesListResponse)
async def get_articles_endpoint(
    request: Request,
    response: Response,
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=10, ge=1, le=60, description="Items per page"),
    search: Optional[str] = Query(default=None, description="Search in title and description"),
    tag: Optional[str] = Query(default=None, description="Filter by tag"),
    pillar: Optional[str] = Query(default=None, description="Filter by editorial pillar id")
):
    """
    Get published articles list for blog page.

    Public endpoint - uses tenant context from middleware.
    Returns only published and active articles for the detected tenant.

    Use case: http://localhost:8080/blog
    """
    tenant_context = get_tenant_context(request)
    return await get_articles_list(
        request, response, tenant_context.tenant_id, page, limit, search, tag, pillar
    )


@router.get("/{slug}", response_model=ArticleDetailResponse)
async def get_article_by_slug_endpoint(
    request: Request,
    slug: str,
    increment_views: bool = Query(default=True, description="Whether to increment view count")
):
    """
    Get a single article by its slug.

    Public endpoint - uses tenant context from middleware.
    Returns full article content for the blog post page.
    Automatically increments view count unless disabled.

    Use case: http://localhost:8080/blog/introduccion-a-nuxt-3
    """
    tenant_context = get_tenant_context(request)
    return await get_article_by_slug(
        request, tenant_context.tenant_id, slug, increment_views
    )


@router.get("/{article_id}/related", response_model=ArticlesListResponse)
async def get_related_articles_endpoint(
    request: Request,
    article_id: int,
    tags: str = Query(..., description="Comma-separated tags to match"),
    limit: int = Query(default=3, ge=1, le=10, description="Number of related articles")
):
    """
    Get related articles based on tags.

    Public endpoint - uses tenant context from middleware.
    Returns articles that share tags with the current article.
    Useful for "Related Posts" section on blog post page.
    """
    tenant_context = get_tenant_context(request)
    return await get_related_articles(tenant_context.tenant_id, article_id, tags, limit)
