from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models import Product
from backend.app.preorder_api import (PreorderAction, PreorderEdit, complete_preorder_withdrawal,
    edit_preorder, manage_preorder, read_preorders, search_preorder_catalog)
from backend.app.product_test_models import ProductTestItem, ProductTestJob
from backend.app.preorder_modes import preorder_item
from backend.app.workspace_context import workspace_context
from tests.test_fast_dumping import _seed_fast_product, _market


def seed(db):
    product, batch, policy, state = _seed_fast_product(db)
    batch.quantity_remaining = 0
    product.merchant_sku = '4671307561'
    item = ProductTestItem(workspace_id=1, product_id=product.id, name='Fertilipan',
        merchant_sku=product.merchant_sku, kaspi_product_id=product.kaspi_product_id,
        input_reference='manual-preorder:' + product.kaspi_product_id,
        kaspi_url='https://kaspi.kz/shop/p/product-123456789/', status='stock_trading',
        test_price_kzt=Decimal(24999), preorder_days=8, stock_count=10,
        offers_json={'stock_mode': True, 'dump_enabled': False})
    db.add(item)
    db.commit()
    return product, batch, policy, state, item


def test_zero_stock_can_resume_same_item_without_duplicate(db_session):
    with workspace_context(1):
        product, batch, policy, state, item = seed(db_session)
        row = read_preorders(db_session)['items'][0]
        assert row['status'] == 'ready_to_resume' and row['can_connect']
        result = edit_preorder(item.id, PreorderEdit(price_kzt=24999, preorder_days=8), db_session)
        assert result['item']['id'] == item.id
        assert result['item']['product_id'] == product.id
        assert not item.offers_json['stock_mode']
        assert read_preorders(db_session)['items'][0]['pending']
        with pytest.raises(HTTPException):
            manage_preorder(item.id, PreorderAction(action='withdraw'), db_session)


def test_catalog_search_pagination_and_workspace_isolation(db_session):
    with workspace_context(1):
        product, _, _, _, item = seed(db_session)
        for index in range(105):
            db_session.add(ProductTestItem(workspace_id=1, name=f'Item {index}', merchant_sku=f'sku{index}',
                kaspi_product_id=str(200000000+index), input_reference=f'manual-preorder:{index}', kaspi_url='url'))
        db_session.commit()
        assert read_preorders(db_session)['total'] == 106
        assert len(read_preorders(db_session, offset=100)['items']) == 6
        assert read_preorders(db_session, q=product.merchant_sku)['items'][0]['id'] == item.id
        assert search_preorder_catalog(db=db_session, q='4671307561')['items'][0]['product_id'] == product.id
        assert search_preorder_catalog(db=db_session, q='%')['total'] == 0
    with workspace_context(2):
        assert read_preorders(db_session)['total'] == 0
        assert search_preorder_catalog(db=db_session)['total'] == 0
        with pytest.raises(HTTPException) as error:
            manage_preorder(item.id, PreorderAction(action='disable'), db_session)
        assert error.value.status_code == 404


def test_disable_preserves_sale_history_and_stays_disabled_on_stock_arrival(db_session):
    from backend.app.preorder_modes import stock_arrived
    with workspace_context(1):
        product, batch, policy, state, item = seed(db_session)
        sale_enabled = product.sale_enabled
        manage_preorder(item.id, PreorderAction(action='disable'), db_session)
        assert not policy.enabled and not item.active
        assert product.sale_enabled == sale_enabled
        assert preorder_item(db_session, product) is None
        batch.quantity_remaining = 4
        db_session.flush()
        assert not stock_arrived(db_session, product.id)
        assert read_preorders(db_session)['items'][0]['status'] == 'preorder_disabled'
        assert not read_preorders(db_session)['items'][0]['can_connect']


def test_withdraw_requires_cabinet_confirmation_and_new_agent(db_session, monkeypatch):
    from backend.app import product_test_api as api
    with workspace_context(1):
        product, _, _, _, item = seed(db_session)
        queued = manage_preorder(item.id, PreorderAction(action='withdraw'), db_session)
        assert not product.sale_enabled and product.sale_state_overridden
        assert read_preorders(db_session)['items'][0]['status'] == 'preorder_withdrawing'
        monkeypatch.setattr(api, '_validate_workspace_merchant', lambda *a, **k: None)
        monkeypatch.setattr(api, '_touch_product_test_agent', lambda *a, **k: None)
        def claim(version):
            return api.claim_product_test_job(api.ProductTestAgentIdentity(agent_id='agent', version=version,
                workspace_id=1, merchant_uid='merchant', agent_kind='product_test'), db_session)
        assert claim('1.1.27')['job'] is None
        assert claim('1.1.28')['job']['id'] == queued['job']['id']
        job = db_session.get(ProductTestJob, queued['job']['id'])
        wrong = {'after': {'found': True, 'sku': 'other', 'master_sku': product.kaspi_product_id, 'row_available': False}}
        with pytest.raises(ValueError):
            complete_preorder_withdrawal(db_session, job=job, result=wrong)
        complete_preorder_withdrawal(db_session, job=job, result={'after': {
            'found': True, 'sku': product.merchant_sku, 'master_sku': product.kaspi_product_id,
            'row_available': False}})
        assert item.status == 'sale_removed' and job.status == 'succeeded'


def test_missing_manual_preorder_never_queues_supplier_reactivation(db_session):
    from backend.app.fast_dumping_service import claim_job, queue_scan, complete_scan
    with workspace_context(1):
        product, batch, policy, state, item = seed(db_session)
        item.offers_json = {'dump_enabled': True, 'stock_mode': False}
        batch.quantity_remaining = 1
        db_session.flush()
        queue_scan(db_session, policy=policy, workspace_id=1, reason='test')
        db_session.flush()
        job = claim_job(db_session, workspace_id=1, agent_id='agent')
        market = _market(own='24999', competitor='26000')
        market['own_price_kzt'] = None
        result = complete_scan(db_session, workspace_id=1, job_id=job.id, agent_id='agent',
            lease_token=job.lease_token, succeeded=True, market_payload=market)
        assert not result['queued_apply']
        assert state.automatic_writes_paused and state.next_scan_at is None


@pytest.mark.parametrize('mismatch', [False, True])
def test_withdraw_http_uses_exact_offer_and_checks_unavailable(monkeypatch, mismatch):
    from tools.product_discovery.kaspi_offer_creator import MerchantOfferApi, OfferState
    api = object.__new__(MerchantOfferApi)
    api.merchant_uid = 'merchant'
    api.store_id = 'store'
    api.city_id = 'city'
    api.session = SimpleNamespace(config=SimpleNamespace(merchant_uid='merchant', assert_live_allowed=lambda live: None))
    before = OfferState(found=True, sku='other' if mismatch else 'sku', master_sku='123456789',
        model='Fertilipan', price_kzt=24999, stock_count=10, preorder_days=8, row_available=True)
    after = OfferState(found=True, sku='sku', master_sku='123456789', row_available=False)
    states = iter([before, after])
    monkeypatch.setattr(api, 'read_offer', lambda sku: next(states))
    writes = []
    def post(*args, **kwargs):
        writes.append(kwargs['json_body'])
        return httpx.Response(200, request=httpx.Request('POST', 'https://kaspi.kz'))
    monkeypatch.setattr(api, '_request_json', post)
    if mismatch:
        with pytest.raises(ValueError):
            api.withdraw_offer(sku='sku', master_sku='123456789', poll_seconds=0)
        assert not writes
    else:
        result = api.withdraw_offer(sku='sku', master_sku='123456789', poll_seconds=0)
        assert result['after']['row_available'] is False
        assert writes[0]['sku'] == 'sku'
        assert writes[0]['availabilities'][0]['available'] == 'no'
        assert writes[0]['availabilities'][0]['stockCount'] == 0


def test_confirmed_legacy_test_preorder_is_visible_and_editable(db_session):
    from datetime import datetime, UTC
    with workspace_context(1):
        product, _, _, _, item = seed(db_session)
        item.input_reference = 'legacy-test'
        item.status = 'enrolled_fast_dumping'
        item.added_at = datetime.now(UTC)
        db_session.commit()
        assert read_preorders(db_session)['items'][0]['id'] == item.id
        edit_preorder(item.id, PreorderEdit(price_kzt=24999, preorder_days=8), db_session)
        assert item.input_reference.startswith('manual-preorder:')


@pytest.mark.parametrize('capable', [False, True])
def test_prepare_endpoint_requires_removal_guard_for_manual_preorders(db_session, monkeypatch, capable):
    from backend.app import fast_dumping_agent_api as api
    from backend.app.fast_dumping_models import FastDumpingJob
    with workspace_context(1):
        product, _, policy, state, item = seed(db_session)
        job = FastDumpingJob(workspace_id=1, product_id=product.id, policy_id=policy.id,
            status='leased_apply', state_version=state.state_version, agent_id='agent', lease_token='a'*32)
        db_session.add(job)
        db_session.flush()
        state.active_job_id = job.id
        monkeypatch.setattr(api, 'prepare_apply', lambda *a, **k: {'ready': True})
        result = api.prepare_job_apply(job.id, api.FastPrepareApply(agent_id='agent', workspace_id=1,
            lease_token='a'*32, respects_removed_offer=capable), db_session)
        assert result['ready'] is capable
        if capable:
            assert result['respect_removed_offer']
        else:
            assert job.status == 'stale' and state.active_job_id is None


@pytest.mark.parametrize('mode', ['inventory', 'preorder'])
def test_fast_agent_never_writes_after_manual_cabinet_removal(monkeypatch, mode):
    import asyncio
    from tools import kaspi_fast_offer_runtime as runtime
    writes, completions = [], []
    live = SimpleNamespace(found=True, query_mode='inactive', row_available=False,
        nested_available='no', stock_count=10, preorder_days=8, price_kzt=24999, pending=False)
    async def post(url, token, payload, **kwargs):
        if url.endswith('prepare-apply'):
            assert payload['respects_removed_offer']
            return {'ready': True, 'respect_removed_offer': True, 'sku': 'sku', 'city_id': 'city',
                'fulfillment_mode': mode, 'stock_count': 10, 'preorder_days': 8,
                'target_price_kzt': '24999', 'model': 'Fertilipan'}
        completions.append(payload)
        return {}
    monkeypatch.setattr(runtime.base, '_post_json_with_retry', post)
    monkeypatch.setattr(runtime.base, '_WRITE_LOCK', asyncio.Lock())
    monkeypatch.setattr(runtime.base, '_log', lambda *a, **k: None)
    monkeypatch.setattr(runtime, 'read_offer_state', lambda *a, **k: live)
    monkeypatch.setattr(runtime, 'write_offer_state', lambda *a, **k: writes.append(k))
    asyncio.run(runtime.process_apply(api_url='https://crm.example', token='test', job={'id': 1, 'lease_token': 'a'*32},
        agent_id='agent', workspace_id=1, merchant_uid='merchant', store_id='store',
        merchant_session=SimpleNamespace(ensure_valid_sid=lambda: None)))
    assert not writes
    assert completions[0]['error_code'] == 'manual_offer_removed'
    assert not completions[0]['accepted']
