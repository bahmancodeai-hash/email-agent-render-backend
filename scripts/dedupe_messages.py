import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402


STABLE_DUPLICATES_SQL = """
WITH grouped AS (
    SELECT
        account_id,
        folder_id,
        CASE
            WHEN nullif(btrim(coalesce(message_id, '')), '') IS NOT NULL
                THEN 'mid:' || lower(btrim(message_id))
            WHEN uid IS NOT NULL
                THEN 'uid:' || uid::text
            ELSE 'fp:' || md5(
                coalesce(subject, '') || '|' ||
                coalesce(from_address, '') || '|' ||
                coalesce(received_at::text, '') || '|' ||
                coalesce(sent_at::text, '') || '|' ||
                coalesce(preview, '')
            )
        END AS dedupe_key,
        count(*) AS duplicate_count
    FROM messages
    GROUP BY account_id, folder_id, dedupe_key
    HAVING count(*) > 1
)
SELECT coalesce(sum(duplicate_count - 1), 0) FROM grouped
"""


DELETE_STABLE_DUPLICATES_SQL = """
WITH duplicate_groups AS (
    SELECT
        account_id,
        folder_id,
        CASE
            WHEN nullif(btrim(coalesce(message_id, '')), '') IS NOT NULL
                THEN 'mid:' || lower(btrim(message_id))
            WHEN uid IS NOT NULL
                THEN 'uid:' || uid::text
            ELSE 'fp:' || md5(
                coalesce(subject, '') || '|' ||
                coalesce(from_address, '') || '|' ||
                coalesce(received_at::text, '') || '|' ||
                coalesce(sent_at::text, '') || '|' ||
                coalesce(preview, '')
            )
        END AS dedupe_key,
        min(created_at) AS first_created_at
    FROM messages
    GROUP BY account_id, folder_id, dedupe_key
    HAVING count(*) > 1
    LIMIT :batch_size
),
keepers AS (
    SELECT DISTINCT ON (messages.account_id, messages.folder_id, duplicate_groups.dedupe_key)
        messages.id,
        messages.account_id,
        messages.folder_id,
        duplicate_groups.dedupe_key
    FROM messages
    JOIN duplicate_groups
        ON duplicate_groups.account_id = messages.account_id
        AND duplicate_groups.folder_id IS NOT DISTINCT FROM messages.folder_id
        AND duplicate_groups.dedupe_key = CASE
            WHEN nullif(btrim(coalesce(messages.message_id, '')), '') IS NOT NULL
                THEN 'mid:' || lower(btrim(messages.message_id))
            WHEN messages.uid IS NOT NULL
                THEN 'uid:' || messages.uid::text
            ELSE 'fp:' || md5(
                coalesce(messages.subject, '') || '|' ||
                coalesce(messages.from_address, '') || '|' ||
                coalesce(messages.received_at::text, '') || '|' ||
                coalesce(messages.sent_at::text, '') || '|' ||
                coalesce(messages.preview, '')
            )
        END
    ORDER BY messages.account_id, messages.folder_id, duplicate_groups.dedupe_key, messages.created_at ASC, messages.id ASC
),
to_delete AS (
    SELECT messages.id
    FROM messages
    JOIN duplicate_groups
        ON duplicate_groups.account_id = messages.account_id
        AND duplicate_groups.folder_id IS NOT DISTINCT FROM messages.folder_id
        AND duplicate_groups.dedupe_key = CASE
            WHEN nullif(btrim(coalesce(messages.message_id, '')), '') IS NOT NULL
                THEN 'mid:' || lower(btrim(messages.message_id))
            WHEN messages.uid IS NOT NULL
                THEN 'uid:' || messages.uid::text
            ELSE 'fp:' || md5(
                coalesce(messages.subject, '') || '|' ||
                coalesce(messages.from_address, '') || '|' ||
                coalesce(messages.received_at::text, '') || '|' ||
                coalesce(messages.sent_at::text, '') || '|' ||
                coalesce(messages.preview, '')
            )
        END
    LEFT JOIN keepers ON keepers.id = messages.id
    WHERE keepers.id IS NULL
),
deleted AS (
    DELETE FROM messages
    WHERE id IN (SELECT id FROM to_delete)
    RETURNING id
)
SELECT count(*) FROM deleted
"""


FINGERPRINT_DUPLICATES_SQL = """
WITH grouped AS (
    SELECT
        account_id,
        folder_id,
        md5(
            coalesce(subject, '') || '|' ||
            coalesce(from_address, '') || '|' ||
            coalesce(to_addresses::text, '') || '|' ||
            coalesce(received_at::text, '') || '|' ||
            coalesce(sent_at::text, '') || '|' ||
            coalesce(preview, '')
        ) AS dedupe_key,
        count(*) AS duplicate_count
    FROM messages
    GROUP BY account_id, folder_id, dedupe_key
    HAVING count(*) > 1
)
SELECT coalesce(sum(duplicate_count - 1), 0) FROM grouped
"""


DELETE_FINGERPRINT_DUPLICATES_SQL = """
WITH duplicate_groups AS (
    SELECT
        account_id,
        folder_id,
        md5(
            coalesce(subject, '') || '|' ||
            coalesce(from_address, '') || '|' ||
            coalesce(to_addresses::text, '') || '|' ||
            coalesce(received_at::text, '') || '|' ||
            coalesce(sent_at::text, '') || '|' ||
            coalesce(preview, '')
        ) AS dedupe_key
    FROM messages
    GROUP BY account_id, folder_id, dedupe_key
    HAVING count(*) > 1
    LIMIT :batch_size
),
keepers AS (
    SELECT DISTINCT ON (messages.account_id, messages.folder_id, duplicate_groups.dedupe_key)
        messages.id,
        messages.account_id,
        messages.folder_id,
        duplicate_groups.dedupe_key
    FROM messages
    JOIN duplicate_groups
        ON duplicate_groups.account_id = messages.account_id
        AND duplicate_groups.folder_id IS NOT DISTINCT FROM messages.folder_id
        AND duplicate_groups.dedupe_key = md5(
            coalesce(messages.subject, '') || '|' ||
            coalesce(messages.from_address, '') || '|' ||
            coalesce(messages.to_addresses::text, '') || '|' ||
            coalesce(messages.received_at::text, '') || '|' ||
            coalesce(messages.sent_at::text, '') || '|' ||
            coalesce(messages.preview, '')
        )
    ORDER BY messages.account_id, messages.folder_id, duplicate_groups.dedupe_key, messages.created_at ASC, messages.id ASC
),
to_delete AS (
    SELECT messages.id
    FROM messages
    JOIN duplicate_groups
        ON duplicate_groups.account_id = messages.account_id
        AND duplicate_groups.folder_id IS NOT DISTINCT FROM messages.folder_id
        AND duplicate_groups.dedupe_key = md5(
            coalesce(messages.subject, '') || '|' ||
            coalesce(messages.from_address, '') || '|' ||
            coalesce(messages.to_addresses::text, '') || '|' ||
            coalesce(messages.received_at::text, '') || '|' ||
            coalesce(messages.sent_at::text, '') || '|' ||
            coalesce(messages.preview, '')
        )
    LEFT JOIN keepers ON keepers.id = messages.id
    WHERE keepers.id IS NULL
),
deleted AS (
    DELETE FROM messages
    WHERE id IN (SELECT id FROM to_delete)
    RETURNING id
)
SELECT count(*) FROM deleted
"""


REFRESH_COUNTS_SQL = """
WITH counts AS (
    SELECT
        account_id,
        count(*) FILTER (WHERE is_deleted = false AND is_draft = false) AS total_messages,
        count(*) FILTER (WHERE is_deleted = false AND is_draft = false AND is_read = false) AS unread_count
    FROM messages
    GROUP BY account_id
)
UPDATE email_accounts AS account
SET
    total_messages = coalesce(counts.total_messages, 0),
    unread_count = coalesce(counts.unread_count, 0),
    updated_at = now()
FROM counts
WHERE account.id = counts.account_id
"""


RESET_EMPTY_ACCOUNT_COUNTS_SQL = """
UPDATE email_accounts AS account
SET total_messages = 0, unread_count = 0, updated_at = now()
WHERE NOT EXISTS (
    SELECT 1 FROM messages WHERE messages.account_id = account.id
)
"""


SAMPLE_SQL = """
WITH ranked AS (
    SELECT
        messages.id,
        email_accounts.email,
        folders.folder_type,
        messages.subject,
        messages.from_address,
        messages.received_at,
        row_number() OVER (
            PARTITION BY
                messages.account_id,
                messages.folder_id,
                CASE
                    WHEN nullif(btrim(coalesce(messages.message_id, '')), '') IS NOT NULL
                        THEN 'mid:' || lower(btrim(messages.message_id))
                    WHEN messages.uid IS NOT NULL
                        THEN 'uid:' || messages.uid::text
                    ELSE 'fp:' || md5(
                        coalesce(messages.subject, '') || '|' ||
                        coalesce(messages.from_address, '') || '|' ||
                        coalesce(messages.received_at::text, '') || '|' ||
                        coalesce(messages.sent_at::text, '') || '|' ||
                        coalesce(messages.preview, '')
                    )
                END
            ORDER BY messages.created_at ASC, messages.id ASC
        ) AS rn
    FROM messages
    JOIN email_accounts ON email_accounts.id = messages.account_id
    LEFT JOIN folders ON folders.id = messages.folder_id
)
SELECT email, folder_type, subject, from_address, received_at
FROM ranked
WHERE rn > 1
ORDER BY received_at DESC NULLS LAST
LIMIT :limit
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove duplicate synced email rows.")
    parser.add_argument("--apply", action="store_true", help="Delete duplicate rows. Without this flag only reports counts.")
    parser.add_argument("--sample", type=int, default=10, help="Number of duplicate examples to print.")
    parser.add_argument("--batch-size", type=int, default=250, help="Duplicate groups to delete per batch.")
    args = parser.parse_args()

    url = settings.database_url_sync or settings.database_url.replace("+asyncpg", "")
    engine = create_engine(url, pool_pre_ping=True)

    with engine.begin() as conn:
        stable_before = conn.execute(text(STABLE_DUPLICATES_SQL)).scalar_one()
        fingerprint_before = conn.execute(text(FINGERPRINT_DUPLICATES_SQL)).scalar_one()
        rows = conn.execute(text(SAMPLE_SQL), {"limit": args.sample}).mappings().all()

        result = {
            "stable_duplicates_before": int(stable_before),
            "fingerprint_duplicates_before": int(fingerprint_before),
            "stable_deleted": 0,
            "fingerprint_deleted": 0,
        }

        if args.apply:
            while True:
                deleted = int(
                    conn.execute(
                        text(DELETE_STABLE_DUPLICATES_SQL),
                        {"batch_size": args.batch_size},
                    ).scalar_one()
                )
                result["stable_deleted"] += deleted
                if deleted == 0:
                    break

            while True:
                deleted = int(
                    conn.execute(
                        text(DELETE_FINGERPRINT_DUPLICATES_SQL),
                        {"batch_size": args.batch_size},
                    ).scalar_one()
                )
                result["fingerprint_deleted"] += deleted
                if deleted == 0:
                    break

            conn.execute(text(REFRESH_COUNTS_SQL))
            conn.execute(text(RESET_EMPTY_ACCOUNT_COUNTS_SQL))

        print(result)
        if rows:
            print("samples:")
            for row in rows:
                print({
                    "email": row["email"],
                    "folder": row["folder_type"],
                    "subject": row["subject"],
                    "from": row["from_address"],
                    "received_at": row["received_at"],
                })


if __name__ == "__main__":
    main()
