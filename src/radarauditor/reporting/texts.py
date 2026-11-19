from pydantic import BaseModel, ConfigDict, Field, ValidationError

from radarauditor.errors import RenderError


class ReviewReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    review_excerpt: str = Field(min_length=1)
    reply: str = Field(min_length=1)


class ReportTexts(BaseModel):
    model_config = ConfigDict(frozen=True)

    introduction: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    subscription_reason: str = Field(min_length=1)
    offer_text: str = Field(min_length=1)
    talk_text: str | None = Field(default=None, min_length=1)
    before_after: str | None = None
    post_suggestions: list[str] = Field(default_factory=list)
    review_replies: list[ReviewReply] = Field(default_factory=list)


def parse_texts(content: str) -> ReportTexts:
    try:
        return ReportTexts.model_validate_json(content)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        raise RenderError(f"texte.json invalid at {location}: {error['msg']}") from exc
