import re
from app.models.message import Message
from app.models.rule import EmailRule, RuleConditionField, RuleConditionOp, RuleActionType


def _match_condition(msg: Message, condition: dict) -> bool:
    field = condition.get("field")
    op = condition.get("op")
    value = condition.get("value", "").lower()

    if field == RuleConditionField.FROM:
        target = (msg.from_address or "").lower()
    elif field == RuleConditionField.SUBJECT:
        target = (msg.subject or "").lower()
    elif field == RuleConditionField.BODY:
        target = ((msg.body_text or "") + (msg.body_html or "")).lower()
    elif field == RuleConditionField.DOMAIN:
        addr = msg.from_address or ""
        target = addr.split("@")[-1].lower() if "@" in addr else ""
    elif field == RuleConditionField.HAS_ATTACHMENT:
        return bool(msg.attachments) == (op == RuleConditionOp.IS_TRUE)
    else:
        return False

    if op == RuleConditionOp.CONTAINS:
        return value in target
    elif op == RuleConditionOp.NOT_CONTAINS:
        return value not in target
    elif op == RuleConditionOp.EQUALS:
        return target == value
    elif op == RuleConditionOp.STARTS_WITH:
        return target.startswith(value)
    elif op == RuleConditionOp.ENDS_WITH:
        return target.endswith(value)
    return False


def matches_rule(msg: Message, rule: EmailRule) -> bool:
    if not rule.conditions:
        return False
    results = [_match_condition(msg, c) for c in rule.conditions]
    if rule.conditions_match == "any":
        return any(results)
    return all(results)


async def apply_rule_actions(msg: Message, rule: EmailRule, db) -> None:
    import uuid
    from sqlalchemy import select
    from app.models.folder import Folder
    from app.models.message import MessageStatus

    for action in rule.actions:
        action_type = action.get("type")
        if action_type == RuleActionType.MARK_READ:
            msg.is_read = True
            msg.status = MessageStatus.READ
        elif action_type == RuleActionType.FLAG:
            msg.is_flagged = True
        elif action_type == RuleActionType.DELETE:
            msg.is_deleted = True
        elif action_type == RuleActionType.ARCHIVE:
            result = await db.execute(
                select(Folder).where(
                    Folder.account_id == msg.account_id,
                    Folder.folder_type == "archive",
                )
            )
            folder = result.scalar_one_or_none()
            if folder:
                msg.folder_id = folder.id
        elif action_type == RuleActionType.MOVE_TO_FOLDER:
            folder_id = action.get("folder_id")
            if folder_id:
                result = await db.execute(
                    select(Folder).where(
                        Folder.id == uuid.UUID(folder_id),
                        Folder.account_id == msg.account_id,
                    )
                )
                folder = result.scalar_one_or_none()
                if folder:
                    msg.folder_id = folder.id
