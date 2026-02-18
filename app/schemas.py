# Pydantic models (request/response DTOs) used by the API layer.
# Keep models minimal and serializable; business logic lives in services/DB.
from pydantic import BaseModel, Field, ConfigDict, field_validator, EmailStr
from typing import Literal, Union, Optional
from datetime import date, datetime


# Base attributes for a property listing (shared by create/read)
class PropertyBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    price_cents: int = Field(..., ge=0)
    requires_approval: bool = False

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: str) -> str:
        # Trim surrounding whitespace before validation
        if isinstance(v, str):
            v = v.strip()
        return v


# Payload for creating a new property
class PropertyCreate(PropertyBase):
    pass


# Response shape when reading a property from the API
class PropertyRead(PropertyBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


# Bookings
# Common booking fields shared by create/read
class BookingBase(BaseModel):
    property_id: int = Field(..., ge=1)
    start_date: date
    end_date: date


# Request payload for creating a booking
class BookingCreate(BookingBase):
    pass


# API response for a booking record
class BookingRead(BookingBase):
    id: int
    guest_id: int
    status: Literal[
        "requested",
        "pending_payment",
        "confirmed",
        "cancelled",
        "cancelled_expired",
        "declined",
    ]
    total_cents: int
    currency: str = "USD"
    expires_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# Booking create response payload
# Next step when host approval is required
class NextActionAwaitApproval(BaseModel):
    type: Literal["await_approval"]


# Next step when payment is required
class NextActionPay(BaseModel):
    type: Literal["pay"]
    expires_at: datetime
    client_secret: str


# Returned after creating a booking; includes the record and the next client action
class BookingCreateResponse(BaseModel):
    booking: BookingRead
    next_action: Union[NextActionAwaitApproval, NextActionPay]


# Payments
# Payment intent details for client-side Stripe PaymentElement
class PaymentInfoResponse(BaseModel):
    booking_id: int
    client_secret: str
    expires_at: datetime

# Authentication and user models

# User roles within the system
Role = Literal["landlord", "tenant"]


# Common user fields shared by create/read
class UserBase(BaseModel):
    email: EmailStr
    role: Role

    # Normalize email input to lowercase without surrounding whitespace
    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip().lower()
        return v


# Request payload for user registration
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    role: Role = "tenant"

    # Normalize email input to lowercase without surrounding whitespace
    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip().lower()
        return v


# API response for a user record
class UserRead(UserBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


# Request payload for logging in
class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)

    # Normalize email input to lowercase without surrounding whitespace
    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip().lower()
        return v


# OAuth2-style token response bundled with the current user profile
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserRead


# Messages
# API response for a chat message
class MessageRead(BaseModel):
    id: int
    property_id: int
    sender_id: int
    text: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# Request payload for sending a message
class MessageCreate(BaseModel):
    property_id: int
    text: str = Field(..., min_length=1, max_length=1000)

    # Trim surrounding whitespace before validation
    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip()
        return v
