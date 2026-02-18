# Property listing endpoints.
# Landlords can manage their own listings; tenants/public can browse all listings.
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from .. import models, schemas
from .auth import require_landlord, get_current_user_optional
from ..rate_limit import rate_limit

# Router namespace for property APIs
router = APIRouter()


@router.get("/properties", response_model=List[schemas.PropertyRead])
def list_properties(db: Session = Depends(get_db), user: Optional[models.User] = Depends(get_current_user_optional)):
    """
    List properties.

    Behavior:
    - If caller is a landlord, return only their properties (owner_id == user.id).
    - Otherwise, return all properties.
    Ordered by newest first.
    """
    if user and user.role == "landlord":
        items = (
            db.query(models.Property)
            .filter(models.Property.owner_id == user.id)
            .order_by(models.Property.id.desc())
            .all()
        )
    else:
        items = db.query(models.Property).order_by(models.Property.id.desc()).all()
    return items


@router.post(
    "/properties",
    response_model=schemas.PropertyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("write"))],
)
def create_property(payload: schemas.PropertyCreate, db: Session = Depends(get_db), user: models.User = Depends(require_landlord)):
    """
    Create a new property owned by the authenticated landlord.

    Validation is handled by Pydantic; this endpoint assigns ownership and persists the record.
    """
    obj = models.Property(
        owner_id=user.id,
        title=payload.title,
        price_cents=payload.price_cents,
        requires_approval=payload.requires_approval,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj
