from pydantic import BaseModel, Field
from typing import List, Optional

class SuggestedString(BaseModel):
    value: Optional[str] = Field(
        description="The suggested string value. Null if no suggestion."
    )
    confidence: float = Field(
        description="Confidence score from 0.0 to 1.0 indicating certainty of the suggestion."
    )

class SuggestedBoolean(BaseModel):
    value: Optional[bool] = Field(
        description="The suggested boolean value. Null if no suggestion."
    )
    confidence: float = Field(
        description="Confidence score from 0.0 to 1.0 indicating certainty of the suggestion."
    )

class TrackMetadataSuggestion(BaseModel):
    track_id: str = Field(
        description="The unique identifier (id) for the track provided in the input batch."
    )
    suggested_language: SuggestedString = Field(
        description="Language suggestion using Wavify values only: english, hindi, tamil, malayalam, indian, spanish, korean, french, or unknown. Use null when evidence is weak."
    )
    suggested_genre: SuggestedString = Field(
        description="Genre suggestion using one Wavify genre bucket only, such as pop, hip-hop, r&b, electronic, rock, folk, bollywood, carnatic, or devotional. Use null instead of inventing hybrid labels."
    )
    suggested_mood: SuggestedString = Field(
        description="Short lowercase mood label if supported by evidence, otherwise null."
    )
    clean_title: SuggestedString = Field(
        description="Cleaned title only when formatting cleanup is obvious; preserve stylized names."
    )
    clean_artist: SuggestedString = Field(
        description="Cleaned artist only when formatting cleanup is obvious; preserve stylized names."
    )
    is_remix_or_live: SuggestedBoolean = Field(
        description="True only when the track is clearly a remix, live version, edit, cover, or alternate version."
    )
    reasoning: str = Field(
        description="A global reasoning string explaining the overall track assessment and choices made."
    )

class BatchMetadataResponse(BaseModel):
    tracks: List[TrackMetadataSuggestion] = Field(
        description="List of metadata suggestions for all tracks in the batch."
    )
