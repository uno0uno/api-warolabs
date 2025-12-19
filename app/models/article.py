# Article models for blog management
from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID
from datetime import datetime


class ArticleBase(BaseModel):
    """Base article fields"""
    title: str = Field(..., min_length=1, max_length=500, description="Article title")
    slug: str = Field(..., min_length=1, max_length=500, description="URL-friendly slug")
    description: str = Field(..., description="Short description")
    content: str = Field(..., description="Full article content (HTML/Markdown)")
    meta_title: str = Field(..., max_length=200, description="SEO meta title")
    meta_descripcion: str = Field(..., max_length=500, description="SEO meta description")
    thumbnail: str = Field(..., description="Thumbnail image URL")
    cover: str = Field(..., description="Cover image URL")
    tags: str = Field(..., description="Comma-separated tags")


class ArticleCreate(ArticleBase):
    """Create article"""
    tenant_id: UUID = Field(..., description="Tenant ID")
    author: UUID = Field(..., description="Author profile ID")
    published: bool = Field(default=False, description="Whether article is published")
    draft: bool = Field(default=True, description="Whether article is a draft")
    is_active: bool = Field(default=True, description="Whether article is active")
    lang: str = Field(default="es", max_length=10, description="Language code")
    planet: str = Field(default="earth", description="Planet (for multi-site)")
    country: str = Field(default="Colombia", description="Country")
    city: str = Field(default="", description="City")


class ArticleUpdate(BaseModel):
    """Update article fields"""
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    slug: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    content: Optional[str] = None
    meta_title: Optional[str] = Field(None, max_length=200)
    meta_descripcion: Optional[str] = Field(None, max_length=500)
    thumbnail: Optional[str] = None
    cover: Optional[str] = None
    tags: Optional[str] = None
    published: Optional[bool] = None
    draft: Optional[bool] = None
    is_active: Optional[bool] = None


class AuthorInfo(BaseModel):
    """Author information from profile"""
    name: Optional[str] = None
    avatar: Optional[str] = None
    user_name: Optional[str] = None
    description: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None

    class Config:
        from_attributes = True


class Article(ArticleBase):
    """Complete article with all fields"""
    id: int
    tenant_id: Optional[UUID] = None
    author: UUID
    id_profile: UUID
    published: bool
    draft: bool
    is_active: bool
    views: Optional[int] = 0
    lang: str
    planet: str
    country: str
    city: str
    created_at: datetime
    updated_at: Optional[datetime] = None

    # Computed fields (from joins)
    author_name: Optional[str] = None
    author_info: Optional[AuthorInfo] = None
    tenant_name: Optional[str] = None

    class Config:
        from_attributes = True


class ArticleSummary(BaseModel):
    """Article summary for list views"""
    id: int
    title: str
    slug: str
    description: str
    thumbnail: str
    cover: str
    tags: str
    views: Optional[int] = 0
    published: bool
    created_at: datetime
    author_name: Optional[str] = None
    author_avatar: Optional[str] = None

    class Config:
        from_attributes = True


class ArticleResponse(BaseModel):
    """Single article response"""
    success: bool = True
    data: Article


class ArticlesListResponse(BaseModel):
    """List of articles response"""
    success: bool = True
    total: int
    data: List[ArticleSummary]


class ArticleDetailResponse(BaseModel):
    """Full article detail response (for blog post page)"""
    success: bool = True
    data: Article
