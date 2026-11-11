from datetime import datetime, UTC, timedelta
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from backend.app import db as database
from backend.app import preorder_snapshot_cache as cache
from backend.app.preorder_api import list_all_preorder_offers
from backend.app.product_test_models import ProductTestItem, ProductTestJob
from backend.app.workspace_context import workspace_context
from tests.test_preorder_inventory import product, applied


def test_inventory_does_not_fetch_market_history_or_serialize_raw_offers(db_session):
    with workspace_context(1):
        p = product(db_session)
        job = applied(db_session, p)
        job.market_json = {'huge_market': 'x'*200000}
        job.write_json = {'huge_write': 'x'*200000}
        db_session.add(ProductTestItem(workspace_id=1, product_id=p.id, merchant_sku=p.merchant_sku,
            kaspi_product_id=p.kaspi_product_id, name=p.name, kaspi_url='url',
            input_reference='manual-preorder:'+p.kaspi_product_id, preorder_days=8,
            test_price_kzt=24999, status='preorder_connected',
            offers_json={'dump_enabled':False,'stock_mode':False,'raw_offers':['x'*200000]}))
        db_session.commit()
        statements=[]
        def capture(conn,cursor,statement,*args):
            statements.append(statement)
        event.listen(db_session.bind,'before_cursor_execute',capture)
        try:
            result=list_all_preorder_offers(db_session)
        finally:
            event.remove(db_session.bind,'before_cursor_execute',capture)
        assert result['items'][0]['test_price_kzt'] == 24999
        assert 'raw_offers' not in result['items'][0]['offers']
        fast_queries=[s for s in statements if 'FROM fast_dumping_jobs' in s]
        assert fast_queries and all('market_json' not in s and 'write_json' not in s for s in fast_queries)
        assert not db_session.in_transaction()


def test_snapshot_refresh_is_single_flight_and_isolated_per_workspace():
    engine=object()
    db=SimpleNamespace(get_bind=lambda:engine)
    started,release=Event(),Event()
    results=[]
    def build():
        started.set(); assert release.wait(2)
        return {'items':[], 'summary':{'total':1}}
    thread=Thread(target=lambda:results.append(cache.inventory_snapshot(db,1,build)))
    thread.start(); assert started.wait(2)
    try:
        with pytest.raises(HTTPException) as exc:
            cache.inventory_snapshot(db,1,lambda:pytest.fail('second database read'))
        assert exc.value.status_code==503
        assert cache.inventory_snapshot(db,2,lambda:{'items':[], 'summary':{'total':2}})['summary']['total']==2
    finally:
        release.set();thread.join(2)
    assert results[0]['summary']['total']==1
    assert cache.inventory_snapshot(db,1,lambda:pytest.fail('duplicate database read'))==results[0]
    assert cache.inventory_snapshot(db,1,lambda:{'items':[], 'summary':{'total':3}},fresh=True)['summary']['total']==3


def test_low_priority_scheduler_does_not_wait_for_exhausted_pool(monkeypatch):
    from backend.app import dumping_competitor_worker as worker
    monkeypatch.setattr(worker,'background_database_available',lambda:False)
    monkeypatch.setattr(worker,'SessionLocal',lambda:pytest.fail('background task acquired a connection'))
    assert worker.dispatch_due_competitor_jobs()==()
    assert worker.SCHEDULER_LAST_RUN['status']=='deferred_pool_busy'


@pytest.mark.parametrize('used,allowed',[(0,True),(3,True),(4,False),(5,False)])
def test_background_capacity_keeps_a_connection_for_interactive_work(monkeypatch,used,allowed):
    monkeypatch.setattr(database,'engine',SimpleNamespace(pool=SimpleNamespace(size=lambda:5,checkedout=lambda:used)))
    assert database.background_database_available() is allowed


def test_explicit_preorder_is_claimed_before_old_discovery(db_session,monkeypatch):
    from backend.app import product_test_api as api
    monkeypatch.setattr(api,'_validate_workspace_merchant',lambda *a,**k:None)
    monkeypatch.setattr(api,'_touch_product_test_agent',lambda *a,**k:None)
    with workspace_context(1):
        old=ProductTestJob(workspace_id=1,job_type='discover',input_reference='old',status='queued',options_json={})
        db_session.add(old);db_session.flush()
        manual=ProductTestJob(workspace_id=1,job_type='inspect',input_reference='new',status='queued',
            options_json={'manual_preorder':True})
        db_session.add(manual);db_session.commit()
        identity=api.ProductTestAgentIdentity(agent_id='agent',workspace_id=1,merchant_uid='merchant',
            agent_kind='product_test',version='1.1.28')
        result=api.claim_product_test_job(identity,db_session)
        assert result['job']['id']==manual.id
        assert old.status=='queued'


@pytest.mark.parametrize('kind,seconds',[('inspect',180),('create_offer',1800),('discover_popular',7200)])
def test_heartbeat_lease_matches_the_job_instead_of_leaving_all_tasks_for_two_hours(db_session,monkeypatch,kind,seconds):
    from backend.app import product_test_api as api
    monkeypatch.setattr(api,'_validate_workspace_merchant',lambda *a,**k:None)
    with workspace_context(1):
        now=datetime.now(UTC)
        job=ProductTestJob(workspace_id=1,job_type=kind,input_reference='task',status='leased',
            agent_id='agent',lease_token='a'*32,lease_until=now+timedelta(hours=2))
        db_session.add(job);db_session.commit()
        result=api.heartbeat_product_test_agent(api.ProductTestAgentHeartbeat(agent_id='agent',
            workspace_id=1,merchant_uid='merchant',agent_kind='product_test',version='1.1.28',
            job_id=job.id,lease_token='a'*32),db_session)
        db_session.refresh(job)
        expires=job.lease_until.replace(tzinfo=UTC) if job.lease_until.tzinfo is None else job.lease_until
        assert result['lease_renewed'] and seconds-1 <= (expires-now).total_seconds() <= seconds+3


def test_recovery_only_requeues_read_only_preorder_inspection_and_caps_retries(db_session):
    from backend.app import product_test_api as api
    with workspace_context(1):
        now=datetime.now(UTC)
        rows=[]
        for kind in ['inspect','create_offer','withdraw_preorder']:
            row=ProductTestJob(workspace_id=1,job_type=kind,input_reference=kind,status='leased',
                agent_id='gone',lease_token='a'*32,lease_until=now+timedelta(hours=2),
                updated_at=now-timedelta(minutes=6),options_json={'manual_preorder':True})
            db_session.add(row);rows.append(row)
        db_session.flush()
        foreign=ProductTestJob(workspace_id=3,job_type='inspect',input_reference='foreign',status='leased',
            lease_until=now-timedelta(seconds=1),options_json={'manual_preorder':True})
        with workspace_context(3):
            db_session.add(foreign);db_session.flush()
        assert api.recover_preorder_inspections(db_session,1,now=now)==1
        assert rows[0].status=='queued' and rows[0].lease_token is None
        assert rows[1].status=='leased' and rows[2].status=='leased' and foreign.status=='leased'
        rows[0].status='leased'
        rows[0].lease_until=now-timedelta(seconds=1)
        rows[0].options_json={'manual_preorder':True,'inspection_recoveries':3}
        db_session.flush()
        assert api.recover_preorder_inspections(db_session,1,now=now)==1
        assert rows[0].status=='failed' and rows[0].error_code=='inspection_recovery_exhausted'
