from enum import Enum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, func, Index, MetaData
from sqlalchemy.dialects.postgresql import JSONB

naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class AbcCustomerEntities(Enum):
    AmoCustomer = 'amo_customers'
    AmoLead = 'amo_leads'
    AmoCompany = 'amo_companies'
    AmoContact = 'amo_contacts'
    SendMethods = 'send_methods'


class AbstractCustomer(db.Model):
    """
    "general" : { "id": int }

    "amo_customers": [
        {"general": {"id": int, "login": str, "billing_type": str,
                     "is_robot": bool, "is_nds": bool, "system": str,
                     "host": str, "name": str},
         "api_value": {}}
    ]
    "amo_leads": [{"general": {"id": int, "login": Optional[str], "name": str}, "api_value": {}}]
    "amo_companies": [{"general": {"id": int, "inn": Optional[str], "name": str}, "api_value": {}}]
    "amo_contacts": [{"general": {"id": int, "phone": Optional[str], "name": str}, "api_value": {}}]
    "send_methods": [{"type": str, "value": str, "desc": str}]
    """
    __tablename__ = "abstract_customer"

    __table_args__ = (
        Index("ix_abc_amo_customers_gin", "amo_customers", postgresql_using="gin"),
        Index("ix_abc_amo_leads_gin",     "amo_leads",     postgresql_using="gin"),
        Index("ix_abc_amo_contacts_gin",  "amo_contacts",  postgresql_using="gin"),
        Index("ix_abc_amo_companies_gin", "amo_companies", postgresql_using="gin"),
    )

    id = Column(db.BigInteger(), nullable=False, primary_key=True)
    amo_customers = Column(JSONB)
    amo_leads = Column(JSONB)
    amo_companies = Column(JSONB)
    amo_contacts = Column(JSONB)
    send_methods = Column(JSONB)
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())