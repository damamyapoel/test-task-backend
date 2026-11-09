# Что было дорого
  - поиск @> по JSONB без индекса — full-scan таблицы на каждый из ~70 000 поисков, итого O(N²), часы.

  - db.session.commit() после каждой сущности — ~70 000 отдельных fsync.

  - UPDATE ... jsonb_set per-row — ~70 000 отдельных UPDATE.

  - в конце — SELECT * FROM abstract_customer в память и filter_merge_abstract_customer с commit() на элемент (плюс for l in entity: внутри цикла — O(N²), и l['general'].get('name').lower() падал на None).

  - баги, ломавшие правила: index = len(...) + 1 (элемент не попадал в массив), commit() внутри create_or_update_lead (нарушал «удалять только после успешного полного прохода»).

# Что изменено
  - models.py — GIN-индексы на 4 JSONB-колонки через __table_args__. На новой базе создаются вместе с таблицей; на существующей нужно накатить один раз вручную. Обслуживают     существующие поиски по точному логину и названию (@>) без изменения формата JSON.

  - main.py — collect_leads_customers переписан:

  - один транзакционный проход, один COMMIT в конце;

  - lookup-карты (customer_id→abc_id, lead_id→abc_id, login→[abc_id]) строятся один раз из узкой выборки; логины дедуплицируются;

  - API обрабатывается потоком, результаты батчами по 2000 через unnest уходят в TEMP-таблицы;

  - слияние сделок: merges{deleted→kept} + resolve() для цепочек; применяется к staging одним INSERT ... SELECT + DELETE на таблицу, затем _delete_merged удаляет сами строки слитых abc;

  - полный проход: пересборка amo_customers/amo_leads — два UPDATE ... (SELECT jsonb_agg ...), фильтрация amo_contacts/amo_companies — два UPDATE с jsonb_array_elements (фильтр по ID и по «удалить» в имени через strpos), затем один DELETE отсутствующих;

  - частичный проход (is_full=False): merge без удаления отсутствующих, с фильтрами status_id (143 / 111349);

  - при падении — rollback, TEMP ... ON COMMIT DROP снимает staging, удаление не выполняется.

  - abstract_customer.py — оставлены только get_customer_json, get_lead_json и get_abccustomer_by_* (используются и новым модулем, и внешним кодом). Удалены update_jsonb_field, dict_compare_merge, create_or_update_customer, create_or_update_lead — они содержали баги и в новом пути не нужны.

# Что оставлено и почему
  - формат JSON в полях — не менялся; внешние части, ищущие по точному логину/названию, работают как раньше.

  - amocrm.py — клиент не входит в задачу.

  - get_abccustomer_by_* — сохранены для внешних вызовов; с GIN это быстрые индексные запросы.

  - поштучный INSERT ... RETURNING id для новых abc — на «ночных» прогонах новых записей почти нет; первый прогон на пустой базе создаст 20 000 строк поштучно (~30–60 с), дальше не триггерится.

# Оценки (20 000 покупателей, 50 000 сделок)
  Память

  - Компонент	                                              Размер
    customer_id_map + lead_id_map (~70k)	                 ~5–7 МБ
    customer_lookup_logins + lead_lookup_logins (~70k)	   ~10–15 МБ
    staging-батч (2000 × ~5–10 КБ)	                       ~10–20 МБ
    Python + Flask + SQLAlchemy	                           ~30–50 МБ
    Итого	                                                 ~60–90 МБ (было ~0.5–1.5 ГБ)
    Запросы к Postgres

  - Фаза	                                                                                           Число execute-вызовов
    CREATE TEMP × 3	                                                                                     3
    Загрузка state (2 × jsonb_array_elements)	                                                           2
    Staging entities (70k / 2000)	                                                                       ~35
    Seen contacts (~70k / 2000)	                                                                         ~35
    Seen companies (~10k / 2000)	                                                                       ~5
    INSERT ... RETURNING новых abc	                                                                     ≈ число новых (стабильно — 0)
    Слияния	                                                                                             ~4 × M (M — число слияний)
    _delete_merged	                                                                                     1
    Rebuild (2 + 2)	                                                                                     4
    Cleanup (только при is_full)	                                                                       1
    COMMIT	                                                                                             1
    Итого (стабильный ночной)	                                                                           ≲ 90
    Итого (первый прогон на пустой БД)	                                                                 ~20 000 (поштучные INSERT RETURNING для новых abc)
Было ~140 000+.
