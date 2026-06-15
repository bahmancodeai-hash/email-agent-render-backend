import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.rule import EmailRule

router = APIRouter()


class RuleCreate(BaseModel):
    name: str
    account_id: str | None = None
    conditions: list[dict]
    conditions_match: str = "all"
    actions: list[dict]
    is_active: bool = True
    stop_processing: bool = False


class RuleOut(BaseModel):
    id: str
    name: str
    is_active: bool
    conditions: list
    conditions_match: str
    actions: list
    times_triggered: int

    class Config:
        from_attributes = True


@router.get("/", response_model=list[RuleOut])
async def list_rules(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailRule)
        .where(EmailRule.user_id == current_user.id)
        .order_by(EmailRule.sort_order)
    )
    return list(result.scalars().all())


@router.post("/", response_model=RuleOut, status_code=201)
async def create_rule(
    data: RuleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rule = EmailRule(
        user_id=current_user.id,
        account_id=uuid.UUID(data.account_id) if data.account_id else None,
        name=data.name,
        conditions=data.conditions,
        conditions_match=data.conditions_match,
        actions=data.actions,
        is_active=data.is_active,
        stop_processing=data.stop_processing,
    )
    db.add(rule)
    await db.flush()
    await db.refresh(rule)
    return rule


@router.patch("/{rule_id}", response_model=RuleOut)
async def update_rule(
    rule_id: uuid.UUID,
    data: RuleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailRule).where(EmailRule.id == rule_id, EmailRule.user_id == current_user.id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.name = data.name
    rule.conditions = data.conditions
    rule.conditions_match = data.conditions_match
    rule.actions = data.actions
    rule.is_active = data.is_active
    return rule


@router.delete("/{rule_id}")
async def delete_rule(
    rule_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(EmailRule).where(EmailRule.id == rule_id, EmailRule.user_id == current_user.id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.delete(rule)
    return {"deleted": True}
