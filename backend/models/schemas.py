from pydantic import BaseModel, Field, field_validator


class InitiateCallRequest(BaseModel):
    phone_number: str = Field(..., description="Payer toll-free number with country code, e.g. +919876543210")
    customer_name: str = Field(default="", description="Legacy alias for patient_name")
    patient_name: str = Field(default="")
    agent_name: str = Field(default="Chris")
    preferred_language: str = Field(default="en-US")
    hospital_name: str = Field(default="")
    claim_id: str = Field(default="")
    claim_type: str = Field(default="Claim Pending")
    tpa_name: str = Field(default="", description="Payer / insurer name (legacy field name)")
    payer_name: str = Field(default="", description="Health insurance company or TPA name")
    npi: str = Field(default="", description="National Provider Identifier for ID&V")
    policy_number: str = Field(default="", description="Patient policy / member number for ID&V")
    claim_age_days: int | None = Field(default=None, description="Days since claim submission (RCM batch filter)")
    scenario_id: str = Field(default="", description="Gold exemplar scenario ID, e.g. TPA-HELP-001")
    tpa_executive_profile: str = Field(
        default="",
        description="TPA executive behavioral profile for adaptive dialogue",
    )
    tpa_sector_id: str = Field(
        default="",
        description="One of 25 TPA sector ids, e.g. claims, eligibility_benefits, denials",
    )
    focus_topics: list[str] = Field(
        default_factory=list,
        description="Optional focus topics within the selected TPA sector",
    )

    @field_validator("claim_age_days", mode="before")
    @classmethod
    def empty_claim_age(cls, v: object) -> int | None:
        if v == "" or v is None:
            return None
        return int(v)  # type: ignore[arg-type]

    @field_validator("focus_topics", mode="before")
    @classmethod
    def coerce_focus_topics(cls, v: object) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    def resolved_payer_name(self) -> str:
        return self.payer_name or self.tpa_name


class InitiateCallResponse(BaseModel):
    call_id: str
    call_sid: str | None = None
    status: str
    message: str


class PublicUrlRequest(BaseModel):
    public_url: str = Field(..., description="Public HTTPS URL, e.g. https://abc.ngrok-free.app")


class PublicUrlResponse(BaseModel):
    public_url: str
    public_url_ready: bool
    public_url_source: str
    message: str


class CallRecord(BaseModel):
    call_id: str
    customer_phone: str
    customer_name: str = ""
    patient_name: str = ""
    hospital_name: str = ""
    claim_id: str = ""
    claim_type: str = ""
    tpa_name: str = ""
    payer_name: str = ""
    npi: str = ""
    policy_number: str = ""
    claim_age_days: int | None = None
    scenario_id: str = ""
    tpa_executive_profile: str = ""
    tpa_sector_id: str = ""
    focus_topics: list[str] = []
    status: str
    lead_score: int = 0
    confidence_score: float = 0.0
    resolution_status: str = ""
    resolution_code: str = ""
    denial_code: str = ""
    pending_documents: list[str] = []
    expected_payment_date: str = ""
    follow_up_date: str = ""
    extracted_data: dict = {}
    transcript: list[dict] = []
    duration_seconds: float = 0
    created_at: float = 0


class VoiceSettingsPatch(BaseModel):
    stability: float | None = Field(default=None, ge=0.0, le=1.0)
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    style: float | None = Field(default=None, ge=0.0, le=1.0)
    speed: float | None = Field(default=None, ge=0.5, le=1.2)
    volume_gain: float | None = Field(default=None, ge=0.7, le=1.4)
    speaker_boost: bool | None = None
    backchannel_enabled: bool | None = None
    twilio_gather_speech_timeout: str | None = None
    low_latency_calls: bool | None = None
