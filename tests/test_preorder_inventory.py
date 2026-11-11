from datetime import datetime, UTC, timedelta
from decimal import Decimal
from sqlalchemy import select, func

from backend.app.models import Product
from backend.app.fast_dumping_models import FastDumpingJob, FastDumpingPolicy, FastDumpingState
from backend.app.dumping_models import DumpingRun, KaspiXmlFeed
from backend.app.product_test_models import ProductTestItem
from backend.app.preorder_api import list_all_preorder_offers
from backend.app.workspace_context import workspace_context


def product(db, sku='sku', master='123456789', workspace=1):
    p = Product(workspace_id=workspace, name='Preorder '+sku, merchant_sku=sku,
        kaspi_product_id=master, sale_enabled=True)
    db.add(p); db.flush()
    return p


def applied(db, p, mode='preorder', stock=5, days=8, at=None):
    policy = db.scalar(select(FastDumpingPolicy).where(FastDumpingPolicy.product_id == p.id))
    if policy is None:
        policy = FastDumpingPolicy(workspace_id=p.workspace_id, product_id=p.id, enabled=True)
        db.add(policy); db.flush()
    j = FastDumpingJob(workspace_id=p.workspace_id, product_id=p.id, policy_id=policy.id,
        status='applied', completed_at=at or datetime.now(UTC),
        decision_json={'fulfillment_mode': mode, 'preorder_days': days, 'stock_count': stock, 'target_price_kzt': '24999'})
    db.add(j); db.flush()
    return j


def feed(db, xml, workspace=1):
    db.add(KaspiXmlFeed(workspace_id=workspace, source_xml=xml, generated_xml=xml, active=True))
    db.flush()


def test_all_preorders_are_listed_without_test_items_or_hundred_row_limit(db_session):
    with workspace_context(1):
        for n in range(121):
            applied(db_session, product(db_session, sku=f'sku-{n}'))
        result = list_all_preorder_offers(db_session)
        assert result['summary']['total'] == 121
        assert result['summary']['active'] == 121
        assert all(r['preorder_days'] == 8 and r['stock_count'] == 5 and r['confirmed'] for r in result['items'])
        assert db_session.scalar(select(func.count()).select_from(ProductTestItem)) == 0


def test_http_inventory_or_zero_state_suppresses_stale_xml_preorder(db_session):
    with workspace_context(1):
        p = product(db_session)
        feed(db_session, '<kaspi_catalog><offers><offer sku="sku"><model>Old</model><price>9900</price><availabilities><availability available="yes" preOrder="8" stockCount="5"/></availabilities></offer></offers></kaspi_catalog>')
        applied(db_session, p)
        applied(db_session, p, mode='inventory', days=0)
        assert list_all_preorder_offers(db_session)['items'] == []


def test_catalog_orphan_classic_and_http_sources_remain_distinct(db_session):
    with workspace_context(1):
        p = product(db_session, sku='classic')
        db_session.add(DumpingRun(workspace_id=1, product_id=p.id, status='published', published=True,
            preorder_days=7, target_price_kzt=Decimal(15000)))
        feed(db_session, '<kaspi_catalog xmlns="urn:kaspi"><offers><offer sku="orphan"><model>Orphan</model><price>12000</price><availabilities><availability available="yes" preOrder="4" stockCount="3"/></availabilities></offer><offer sku="removed"><model>Removed</model><availabilities><availability available="no" preOrder="5"/></availabilities></offer></offers></kaspi_catalog>')
        applied(db_session, product(db_session, sku='http'))
        result = list_all_preorder_offers(db_session)
        assert {r['source'] for r in result['items']} == {'fast', 'classic', 'catalog'}
        orphan = next(r for r in result['items'] if r['merchant_sku'] == 'orphan')
        assert orphan['product_id'] is None and orphan['preorder_days'] == 4 and orphan['stock_count'] == 3
        assert not orphan['confirmed'] and orphan['can_connect']


def test_same_master_different_merchant_offers_not_collapsed_and_no_workspace_leak(db_session):
    with workspace_context(1):
        applied(db_session, product(db_session, sku='one'))
        applied(db_session, product(db_session, sku='two'))
    with workspace_context(2):
        applied(db_session, product(db_session, sku='foreign', workspace=2))
        assert len(list_all_preorder_offers(db_session)['items']) == 1
    with workspace_context(1):
        assert {r['merchant_sku'] for r in list_all_preorder_offers(db_session)['items']} == {'one', 'two'}


def test_manual_and_fast_same_product_appear_once_and_latest_inventory_is_not_active(db_session):
    now = datetime.now(UTC)
    with workspace_context(1):
        p = product(db_session)
        db_session.add(ProductTestItem(workspace_id=1, product_id=p.id,
            input_reference='manual-preorder:123456789', merchant_sku='sku', kaspi_product_id='123456789',
            name=p.name, kaspi_url='url', added_at=now-timedelta(days=1), status='preorder_connected',
            preorder_days=8, stock_count=5, test_price_kzt=Decimal(24999), offers_json={}))
        applied(db_session, p, at=now-timedelta(hours=12))
        result = list_all_preorder_offers(db_session)
        assert result['summary']['total'] == 1 and result['summary']['active'] == 1
        applied(db_session, p, mode='inventory', days=0, at=now)
        result = list_all_preorder_offers(db_session)
        assert result['summary']['total'] == 1 and result['summary']['active'] == 0
        assert result['items'][0]['status'] == 'ready_to_resume'


def test_supplier_estimate_is_pending_not_a_confirmed_active_preorder(db_session):
    with workspace_context(1):
        p = product(db_session)
        policy = FastDumpingPolicy(workspace_id=1, product_id=p.id)
        db_session.add(policy); db_session.flush()
        db_session.add(FastDumpingState(workspace_id=1, product_id=p.id, policy_id=policy.id,
            source_kind='supplier', desired_stock_count=5, own_price_kzt=Decimal(24999)))
        db_session.flush()
        result = list_all_preorder_offers(db_session)
        assert result['summary']['working'] == 1 and result['summary']['active'] == 0
        assert result['items'][0]['preorder_days'] is None and not result['items'][0]['confirmed']


def test_invalid_old_xml_does_not_hide_http_preorders(db_session):
    with workspace_context(1):
        applied(db_session, product(db_session))
        feed(db_session, '<broken')
        result = list_all_preorder_offers(db_session)
        assert result['summary']['total'] == 1 and result['warnings']


def test_newer_classic_inventory_supersedes_earlier_http_preorder(db_session):
    with workspace_context(1):
        p = product(db_session)
        applied(db_session, p, at=datetime.now(UTC)-timedelta(days=1))
        db_session.add(DumpingRun(workspace_id=1, product_id=p.id, status='published', published=True,
            preorder_days=0, created_at=datetime.now(UTC)))
        db_session.flush()
        assert list_all_preorder_offers(db_session)['items'] == []
