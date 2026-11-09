import json
import logging
from typing import Dict, List, Optional

from sqlalchemy import text

from abstract_customer import get_customer_json, get_lead_json
from amocrm import Customer, Lead, COMPANIES, CONTACTS, amocrm_get
from models import db

logger = logging.getLogger(__name__)
BATCH = 2000


class SyncContext:
    """
    id → abc_id и login → [abc_id]
    JSON (api_value) батчами уходят в staging

    """
    def __init__(self):
        self.customer_id_map: Dict[int, int] = {}                 # amo customer id → abc_id
        self.lead_id_map: Dict[int, int] = {}                     # amo lead id     → abc_id
        self.customer_lookup_logins: Dict[str, List[int]] = {}    # логины из amo_leads
        self.lead_lookup_logins: Dict[str, List[int]] = {}        # логины из amo_customers
        self.merges: Dict[int, int] = {}                          # deleted_abc_id → kept_abc_id
        self._stage: List[dict] = []
        self._contacts: List[dict] = []
        self._companies: List[dict] = []

    def resolve(self, abc_id: int) -> int:
        """ цепочки merge A → B → C """
        seen = set()
        while abc_id in self.merges and abc_id not in seen:
            seen.add(abc_id)
            abc_id = self.merges[abc_id]
        return abc_id

    @staticmethod
    def _unique(mapping: Dict[str, List[int]], login: Optional[str]) -> Optional[int]:
        if not login:
            return None
        candidates = mapping.get(login, [])
        return candidates[0] if len(candidates) == 1 else None

    def new_abstract_customer(self) -> int:
        return db.session.execute(text(
            "INSERT INTO abstract_customer (amo_customers, amo_leads, amo_companies, amo_contacts) "
            "VALUES ('[]'::jsonb, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb) RETURNING id"
        )).scalar_one()

    def stage_entity(self, abc_id: int, kind: str, amo_id: int, payload: dict):
        self._stage.append({"abc_id": abc_id, "kind": kind,
                            "amo_id": amo_id, "payload": json.dumps(payload)})
        if len(self._stage) >= BATCH:
            self.flush_stage()

    def flush_stage(self):
        if not self._stage:
            return
        rows = self._stage
        db.session.execute(text("""
            INSERT INTO stage_entities (abc_id, kind, amo_id, payload)
            SELECT u.abc_id, u.kind, u.amo_id, u.payload::jsonb
            FROM unnest(
                CAST(:abc_ids AS bigint[]),
                CAST(:kinds   AS text[]),
                CAST(:amo_ids AS bigint[]),
                CAST(:payloads AS text[])
            ) AS u(abc_id, kind, amo_id, payload)
            ON CONFLICT (abc_id, kind, amo_id) DO UPDATE SET payload = EXCLUDED.payload
        """), {
            "abc_ids": [r["abc_id"] for r in rows],
            "kinds":   [r["kind"] for r in rows],
            "amo_ids": [r["amo_id"] for r in rows],
            "payloads": [r["payload"] for r in rows],
        })
        self._stage.clear()

    def seen_contact(self, abc_id: int, entity_id: int):
        self._contacts.append({"abc_id": abc_id, "entity_id": entity_id})
        if len(self._contacts) >= BATCH:
            self.flush_seen()

    def seen_company(self, abc_id: int, entity_id: int):
        self._companies.append({"abc_id": abc_id, "entity_id": entity_id})
        if len(self._companies) >= BATCH:
            self.flush_seen()

    def flush_seen(self):
        if self._contacts:
            db.session.execute(text("""
                INSERT INTO stage_contacts (abc_id, entity_id)
                SELECT u.abc_id, u.entity_id
                FROM unnest(CAST(:abc_ids AS bigint[]), CAST(:entity_ids AS bigint[]))
                    AS u(abc_id, entity_id)
                ON CONFLICT DO NOTHING
            """), {
                "abc_ids":   [r["abc_id"] for r in self._contacts],
                "entity_ids": [r["entity_id"] for r in self._contacts],
            })
            self._contacts.clear()
        if self._companies:
            db.session.execute(text("""
                INSERT INTO stage_companies (abc_id, entity_id)
                SELECT u.abc_id, u.entity_id
                FROM unnest(CAST(:abc_ids AS bigint[]), CAST(:entity_ids AS bigint[]))
                    AS u(abc_id, entity_id)
                ON CONFLICT DO NOTHING
            """), {
                "abc_ids":   [r["abc_id"] for r in self._companies],
                "entity_ids": [r["entity_id"] for r in self._companies],
            })
            self._companies.clear()


def _setup_temp_tables():
    db.session.execute(text("""
        CREATE TEMP TABLE stage_entities (
            abc_id BIGINT NOT NULL, kind TEXT NOT NULL, amo_id BIGINT NOT NULL,
            payload JSONB NOT NULL, PRIMARY KEY (abc_id, kind, amo_id)
        ) ON COMMIT DROP
    """))
    db.session.execute(text("""
        CREATE TEMP TABLE stage_contacts (
            abc_id BIGINT NOT NULL, entity_id BIGINT NOT NULL,
            PRIMARY KEY (abc_id, entity_id)
        ) ON COMMIT DROP
    """))
    db.session.execute(text("""
        CREATE TEMP TABLE stage_companies (
            abc_id BIGINT NOT NULL, entity_id BIGINT NOT NULL,
            PRIMARY KEY (abc_id, entity_id)
        ) ON COMMIT DROP
    """))


def _load_state(ctx: SyncContext):
    # покупатели: id → abc_id и логины из amo_leads → [abc_id]
    for abc_id, elem_text in db.session.execute(text(
        "SELECT ac.id, e::text FROM abstract_customer ac, "
        "LATERAL jsonb_array_elements(COALESCE(ac.amo_customers, '[]'::jsonb)) e"
    )):
        elem = json.loads(elem_text)
        general = elem.get("general") or {}
        amo_id = general.get("id")
        login = general.get("login")
        if amo_id is not None:
            ctx.customer_id_map[int(amo_id)] = abc_id
        if login:
            lst = ctx.lead_lookup_logins.setdefault(login, [])
            if abc_id not in lst:
                lst.append(abc_id)

    # сделки: id → abc_id и логины из amo_customers → [abc_id]
    for abc_id, elem_text in db.session.execute(text(
        "SELECT ac.id, e::text FROM abstract_customer ac, "
        "LATERAL jsonb_array_elements(COALESCE(ac.amo_leads, '[]'::jsonb)) e"
    )):
        elem = json.loads(elem_text)
        general = elem.get("general") or {}
        amo_id = general.get("id")
        login = general.get("login")
        if amo_id is not None:
            ctx.lead_id_map[int(amo_id)] = abc_id
        if login:
            lst = ctx.customer_lookup_logins.setdefault(login, [])
            if abc_id not in lst:
                lst.append(abc_id)


def _process_customers(ctx: SyncContext, is_full: bool):
    for customer in amocrm_get(Customer, _with=f"{COMPANIES},{CONTACTS}"):
        if not is_full and customer.status_id == 111349:
            continue
        login = customer.get_field("Логин")
        if not login:
            continue  # покупатель без логина пропускается

        by_id = ctx.customer_id_map.get(customer.id)
        if by_id is not None:
            by_id = ctx.resolve(by_id)

        if by_id is not None:
            target = by_id
        else:
            bl = ctx._unique(ctx.customer_lookup_logins, login)
            by_login = ctx.resolve(bl) if bl is not None else None
            if by_login is not None:
                target = by_login
            else:
                target = ctx.new_abstract_customer()
                lst = ctx.lead_lookup_logins.setdefault(login, [])
                if target not in lst:
                    lst.append(target)

        ctx.customer_id_map[customer.id] = target
        lst = ctx.lead_lookup_logins.setdefault(login, [])
        if target not in lst:
            lst.append(target)

        ctx.stage_entity(target, "customer", customer.id, get_customer_json(customer))
        for c in customer.contacts:
            ctx.seen_contact(target, c["id"])
        for co in customer.companies:
            ctx.seen_company(target, co["id"])

    ctx.flush_stage()
    ctx.flush_seen()


def _process_leads(ctx: SyncContext, is_full: bool):
    for lead in amocrm_get(Lead, _with=CONTACTS):
        if not is_full and lead.status_id == 143:
            continue
        login = lead.get_field("Логин")

        by_id = ctx.lead_id_map.get(lead.id)
        if by_id is not None:
            by_id = ctx.resolve(by_id)
        bl = ctx._unique(ctx.lead_lookup_logins, login)
        by_login = ctx.resolve(bl) if bl is not None else None

        if by_id is not None and by_login is not None and by_id != by_login:
            # «найденную по логину» в «найденную по id»
            ctx.merges[by_login] = by_id
            target = by_id
        elif by_id is not None:
            target = by_id
        elif by_login is not None:
            target = by_login
        else:
            target = ctx.new_abstract_customer()

        ctx.lead_id_map[lead.id] = target
        if login:
            lst = ctx.customer_lookup_logins.setdefault(login, [])
            if target not in lst:
                lst.append(target)

        ctx.stage_entity(target, "lead", lead.id, get_lead_json(lead))
        for c in lead.contacts:
            ctx.seen_contact(target, c["id"])

    ctx.flush_stage()
    ctx.flush_seen()


def _apply_merges(ctx: SyncContext):
    """ перенос staged-строк из «удаляемых» abc в «оставляемые» """
    for deleted, kept in list(ctx.merges.items()):
        kept = ctx.resolve(kept)
        if kept == deleted:
            continue
        db.session.execute(text("""
            INSERT INTO stage_entities (abc_id, kind, amo_id, payload)
            SELECT :kept, kind, amo_id, payload FROM stage_entities WHERE abc_id = :deleted
            ON CONFLICT (abc_id, kind, amo_id) DO NOTHING
        """), {"kept": kept, "deleted": deleted})
        db.session.execute(text(
            "DELETE FROM stage_entities WHERE abc_id = :deleted"),
            {"deleted": deleted})
        for t in ("stage_contacts", "stage_companies"):
            db.session.execute(text(f"""
                INSERT INTO {t} (abc_id, entity_id)
                SELECT :kept, entity_id FROM {t} WHERE abc_id = :deleted
                ON CONFLICT DO NOTHING
            """), {"kept": kept, "deleted": deleted})
            db.session.execute(text(
                f"DELETE FROM {t} WHERE abc_id = :deleted"),
                {"deleted": deleted})


def _delete_merged(ctx: SyncContext):
    """ удаление строк слитых abc из основной таблицы """
    if not ctx.merges:
        return
    deleted_ids = list(ctx.merges.keys())
    db.session.execute(
        text("DELETE FROM abstract_customer WHERE id = ANY(CAST(:ids AS bigint[]))"),
        {"ids": deleted_ids},
    )


def _rebuild_full():
    """ пересбррка массивов из staging (полный проход) """
    for kind, col in (("customer", "amo_customers"), ("lead", "amo_leads")):
        db.session.execute(text(f"""
            UPDATE abstract_customer ac
            SET {col} = COALESCE((
                SELECT jsonb_agg(payload ORDER BY amo_id)
                FROM stage_entities se
                WHERE se.abc_id = ac.id AND se.kind = '{kind}'
            ), '[]'::jsonb)
            WHERE ac.id IN (SELECT DISTINCT abc_id FROM stage_entities)
        """))
    for col, stage in (("amo_contacts", "stage_contacts"),
                       ("amo_companies", "stage_companies")):
        db.session.execute(text(f"""
            UPDATE abstract_customer ac
            SET {col} = COALESCE((
                SELECT jsonb_agg(elem)
                FROM jsonb_array_elements(COALESCE(ac.{col}, '[]'::jsonb)) elem
                WHERE (elem->'general'->>'id')::bigint IN (
                    SELECT entity_id FROM {stage} WHERE abc_id = ac.id
                )
                AND strpos(lower(coalesce(elem->'general'->>'name','')), 'удалить') = 0
            ), '[]'::jsonb)
            WHERE ac.id IN (SELECT DISTINCT abc_id FROM stage_entities)
        """))


def _merge_partial():
    """ частичный проход, элементы не из этой выгрузки сохранены """
    for kind, col in (("customer", "amo_customers"), ("lead", "amo_leads")):
        db.session.execute(text(f"""
            UPDATE abstract_customer ac
            SET {col} = (
                SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb)
                FROM (
                    SELECT elem
                    FROM jsonb_array_elements(COALESCE(ac.{col}, '[]'::jsonb)) elem
                    WHERE (elem->'general'->>'id')::bigint NOT IN (
                        SELECT amo_id FROM stage_entities
                        WHERE abc_id = ac.id AND kind = '{kind}'
                    )
                    UNION ALL
                    SELECT payload FROM stage_entities
                    WHERE abc_id = ac.id AND kind = '{kind}'
                ) all_elems
            )
            WHERE ac.id IN (SELECT DISTINCT abc_id FROM stage_entities)
        """))


def _cleanup():
    db.session.execute(text(
        "DELETE FROM abstract_customer "
        "WHERE id NOT IN (SELECT DISTINCT abc_id FROM stage_entities)"))


def collect_leads_customers(is_full: bool = True) -> None:
    """
    точка входа ночной синхронизации:

    полный проход (is_full=True):
        — покупатели без логина пропускаются;
        — покупатель: по id CRM, иначе по логину среди сделок (неоднозначный логин — скип);
        — сделка: по id CRM, иначе по логину среди покупателей; при конфликте две записи
          сливаются в «по id», вторая удаляется; ничего не нашли — создаём;
        — при успехе пересобираем массивы из staging, чистим отсутствующих и «удалить»;
        — одна транзакция: если что-то упало — rollback, удаление не запускается.

    частичный проход (is_full=False):
        — фильтр по status_id (покупатель 111349, сделка 143);
        — те же правила поиска и слияния, без удаления отсутствующих.
    """
    ctx = SyncContext()
    try:
        _setup_temp_tables()
        _load_state(ctx)
        logger.info("state loaded: customers=%d leads=%d",
                    len(ctx.customer_id_map), len(ctx.lead_id_map))

        _process_customers(ctx, is_full=is_full)
        _process_leads(ctx, is_full=is_full)
        _apply_merges(ctx)
        _delete_merged(ctx)

        if is_full:
            _rebuild_full()
            _cleanup()
        else:
            _merge_partial()

        db.session.commit()
        logger.info("sync done, is_full=%s", is_full)
    except Exception:
        db.session.rollback()
        logger.exception("sync failed, transaction rolled back")
        raise