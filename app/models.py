"""Data classes for the Nexoryn Agent CLI.

Post/Comment/Message mirror the SQLite schema in db.py (plain dataclasses,
matching db.py's existing row<->dataclass mapping style). Summary and its
nested pieces are pydantic models instead, since they're the CLI's JSON
output contract -- pydantic gives validation and `.model_dump_json()` for
free, which matters for "JSON output is ALWAYS valid and structured
exactly as shown."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ---- raw Facebook data (DB row shapes) ----


@dataclass
class Post:
    id: str
    message: str
    created_time: str
    reactions_count: int = 0
    comments_count: int = 0
    permalink_url: Optional[str] = None
    engagement_score: Optional[int] = None
    fetched_at: Optional[str] = None


@dataclass
class Comment:
    id: str
    post_id: str
    text: str
    author_name: str
    created_time: str
    author_id: Optional[str] = None
    # True once a reply authored by the Page exists under this (top-level)
    # comment -- this is what "unanswered" means in summary_generator.py.
    has_page_reply: bool = False
    sentiment: Optional[str] = None
    fetched_at: Optional[str] = None


@dataclass
class Message:
    id: str
    conversation_id: str
    sender_id: str
    text: str
    created_time: str
    sender_name: Optional[str] = None
    is_from_page: bool = False
    theme: Optional[str] = None
    fetched_at: Optional[str] = None


# ---- output shapes (the CLI's JSON contract) ----


class UnansweredComment(BaseModel):
    post_id: str
    post_title: str
    comment_text: str
    author: str
    time_since: str


class LowEngagementPost(BaseModel):
    post_id: str
    title: str
    engagement_score: int
    reactions: int
    comments: int
    posted_ago: str


class DMTheme(BaseModel):
    """Internal working model used while counting/grouping message themes,
    before flattening into the `dm_themes` dict in Summary's output."""

    name: str
    count: int
    percentage: int


class Recommendation(BaseModel):
    text: str


class Summary(BaseModel):
    timestamp: str
    status: str  # "healthy" | "action_needed"
    message: str
    unanswered_comments: List[UnansweredComment] = Field(default_factory=list)
    low_engagement_posts: List[LowEngagementPost] = Field(default_factory=list)
    dm_themes: Dict[str, int] = Field(default_factory=dict)
    recommendations: List[str] = Field(default_factory=list)
    total_action_items: int = 0
