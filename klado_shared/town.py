"""Persistent town addresses, one identity per code, and office-scoped mailboxes.

Only additive migrations: old codes remain valid and unattributable old work is
kept on a named legacy agent, never copied onto every new agent.
"""
from datetime import datetime, timezone
import psycopg2.extras
from . import accounts

_READY = False
DDL = """
CREATE TABLE IF NOT EXISTS town_buildings (
 id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('personal','team')),
 capacity INT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS town_offices (
 id BIGSERIAL PRIMARY KEY, owner_key TEXT UNIQUE NOT NULL,
 building_id BIGINT NOT NULL REFERENCES town_buildings(id), unit INT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE(building_id,unit)
);
CREATE TABLE IF NOT EXISTS office_agents (
 id BIGSERIAL PRIMARY KEY, user_id INT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
 token_id INT UNIQUE REFERENCES agent_tokens(id) ON DELETE SET NULL,
 legacy_user_id INT UNIQUE REFERENCES app_users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, animal TEXT NOT NULL, cloth TEXT NOT NULL,
 last_seen_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS office_agents_owner ON office_agents(user_id,id);
ALTER TABLE office_updates ADD COLUMN IF NOT EXISTS agent_id BIGINT REFERENCES office_agents(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS office_updates_agent_time ON office_updates(agent_id,id DESC);
CREATE TABLE IF NOT EXISTS agent_messages (
 id BIGSERIAL PRIMARY KEY, office_id BIGINT NOT NULL REFERENCES town_offices(id),
 sender_id BIGINT NOT NULL REFERENCES office_agents(id) ON DELETE CASCADE,
 recipient_id BIGINT NOT NULL REFERENCES office_agents(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('message','handoff')), subject TEXT NOT NULL, body TEXT NOT NULL,
 client_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
 CHECK(state IN ('pending','read','accepted','completed','declined')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE(sender_id,client_id), CHECK(sender_id <> recipient_id)
);
CREATE INDEX IF NOT EXISTS agent_messages_inbox ON agent_messages(recipient_id,id DESC);
CREATE INDEX IF NOT EXISTS agent_messages_office_time ON agent_messages(office_id,updated_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS agent_messages_pending ON agent_messages(recipient_id,id) WHERE state='pending';
"""


def ensure_schema():
    global _READY
    if _READY:
        return
    accounts._ensure_schema()
    with accounts._db() as c, c.cursor() as q:
        q.execute('SELECT pg_advisory_xact_lock(62006101)')
        q.execute(DDL)
    _READY = True


def _rows(q):
    return [dict(r) for r in q.fetchall()]


def _members(q):
    q.execute("""SELECT u.id,u.display_name,u.avatar_animal,u.avatar_cloth,u.created_at,
        CASE WHEN o.kind='enterprise' THEN 'team:'||o.id ELSE 'user:'||u.id END AS owner_key,
        CASE WHEN o.kind='enterprise' THEN o.name ELSE u.display_name END AS office_name
        FROM app_users u LEFT JOIN org_members m ON m.user_id=u.id
        LEFT JOIN orgs o ON o.id=m.org_id
        WHERE NOT u.disabled AND u.deleted_at IS NULL
        AND (m.id IS NULL OR (m.status='active' AND o.status='active'))
        ORDER BY u.created_at,u.id""")
    return _rows(q)


def _sync(q):
    # One transaction serialises new allocations across all web workers.
    q.execute('SELECT pg_advisory_xact_lock(62006102)')
    members = _members(q)
    q.execute('SELECT * FROM town_offices ORDER BY id')
    places = {r['owner_key']:dict(r) for r in q.fetchall()}
    for u in members:
        key=u['owner_key']
        if key in places:
            continue
        kind='team' if key.startswith('team:') else 'personal'
        q.execute("""SELECT b.id,b.capacity,COALESCE(MAX(o.unit),0)::int AS used FROM town_buildings b
            LEFT JOIN town_offices o ON o.building_id=b.id WHERE b.kind=%s
            GROUP BY b.id HAVING COALESCE(MAX(o.unit),0)<b.capacity ORDER BY b.id LIMIT 1""",(kind,))
        building=q.fetchone()
        if not building:
            q.execute('INSERT INTO town_buildings(kind,capacity) VALUES(%s,%s) RETURNING id,0 AS used',
                      (kind,12 if kind=='personal' else 1))
            building=q.fetchone()
        q.execute('INSERT INTO town_offices(owner_key,building_id,unit) VALUES(%s,%s,%s) RETURNING *',
                  (key,building['id'],building['used']+1))
        places[key]=dict(q.fetchone())
    q.execute("""INSERT INTO office_agents(user_id,token_id,name,animal,cloth,created_at)
        SELECT t.user_id,t.id,COALESCE(NULLIF(trim(t.label),''),'Agent '||t.id),u.avatar_animal,u.avatar_cloth,t.created_at
        FROM agent_tokens t JOIN app_users u ON u.id=t.user_id
        WHERE NOT EXISTS(SELECT 1 FROM office_agents a WHERE a.token_id=t.id)
        ORDER BY t.created_at,t.id
        ON CONFLICT(token_id) DO NOTHING""")
    # Old account-level events cannot be attributed to a particular code.
    q.execute("""INSERT INTO office_agents(user_id,legacy_user_id,name,animal,cloth)
        SELECT u.id,u.id,'Legacy agent',u.avatar_animal,u.avatar_cloth FROM app_users u
        WHERE EXISTS(SELECT 1 FROM office_updates w WHERE w.user_id=u.id AND w.agent_id IS NULL)
        ON CONFLICT(legacy_user_id) DO NOTHING""")
    q.execute("""UPDATE office_updates w SET agent_id=a.id FROM office_agents a
        WHERE w.agent_id IS NULL AND a.legacy_user_id=w.user_id""")
    return members,places


def _context(q,user_id):
    members,places=_sync(q)
    me=next((u for u in members if u['id']==user_id),None)
    if not me:
        raise PermissionError('Office access is unavailable')
    mine=places[me['owner_key']]
    own=[u['id'] for u in members if u['owner_key']==me['owner_key']]
    return members,places,me,mine,own


def _office(me,mine):
    return dict(id=mine['id'],building_id=mine['building_id'],unit=mine['unit'],
                floor=(mine['unit']-1)//2+1,kind='team' if me['owner_key'].startswith('team:') else 'personal',
                name=me['office_name'] or 'Office')


def town(user_id,offset=0,limit=120):
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        members,places,me,mine,own=_context(q,user_id)
        occupied={places[u['owner_key']]['id'] for u in members}
        q.execute('SELECT * FROM town_buildings ORDER BY id')
        all_buildings=_rows(q)
        page=all_buildings[offset:offset+limit]
        # Always include the caller's address, even beyond the visible district page.
        if not any(b['id']==mine['building_id'] for b in page):
            page += [b for b in all_buildings if b['id']==mine['building_id']]
        buildings=[]
        for b in page:
            offices=[p for p in places.values() if p['building_id']==b['id'] and p['id'] in occupied]
            buildings.append(dict(id=b['id'],kind=b['kind'],capacity=b['capacity'],occupied=len(offices),
                                  mine=b['id']==mine['building_id']))
        return dict(buildings=buildings,office=_office(me,mine),total_buildings=len(all_buildings),
                    total_offices=len(occupied),next_offset=offset+limit if offset+limit<len(all_buildings) else None)


def _agent_rows(q,users):
    q.execute("""SELECT a.*,t.revoked_at,t.last_used_at,
        (a.token_id IS NULL AND a.legacy_user_id IS NULL OR t.revoked_at IS NOT NULL) AS revoked,
        GREATEST(a.last_seen_at,t.last_used_at,w.last_at) AS seen,
        COALESCE(w.updates,'[]'::json) AS updates
        FROM office_agents a LEFT JOIN agent_tokens t ON t.id=a.token_id
        LEFT JOIN LATERAL (SELECT MAX(e.created_at) AS last_at,json_agg(e ORDER BY id DESC) AS updates
          FROM (SELECT id,state,task,summary,created_at FROM office_updates
                WHERE agent_id=a.id ORDER BY id DESC LIMIT 12) e) w ON TRUE
        WHERE a.user_id=ANY(%s) ORDER BY a.id""",(users,))
    return _rows(q)


def _public_agent(a,caller_id):
    now=datetime.now(timezone.utc);seen=a['seen'];updates=a['updates']
    active=bool(seen and (now-seen).total_seconds()<900 and not a['revoked'])
    return dict(agent_id=a['id'],user_id=a['user_id'],name=a['name'],role='agent',
        avatar=dict(animal=a['animal'],cloth=a['cloth']),joined_at=a['created_at'].isoformat(),
        presence='active' if active else ('idle' if seen else 'never'),
        agent_last_at=seen.isoformat() if seen else None,agent_hits=1 if seen else 0,
        updates=updates,work_stale=bool(updates and (now-datetime.fromisoformat(updates[0]['created_at'])).total_seconds()>=900),
        revoked=bool(a['revoked']),legacy=bool(a['legacy_user_id']),editable=a['user_id']==caller_id)


def office_agents(user_id,office_id=None):
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        _,_,me,mine,own=_context(q,user_id)
        if office_id is not None and office_id!=mine['id']:
            raise PermissionError('This office is private')
        return dict(office=_office(me,mine),seats=[_public_agent(a,user_id) for a in _agent_rows(q,own)],
                    truncated=False,active_window_min=15)


def _actor(q,user):
    token_id=user.get('_agent_token_id')
    if token_id:
        q.execute('SELECT * FROM office_agents WHERE token_id=%s AND user_id=%s',(token_id,user['id']))
    else:
        # Basic auth has no individual code identity: retain one explicit legacy desk.
        q.execute("""INSERT INTO office_agents(user_id,legacy_user_id,name,animal,cloth)
            SELECT id,id,'Legacy agent',avatar_animal,avatar_cloth FROM app_users WHERE id=%s
            ON CONFLICT(legacy_user_id) DO NOTHING""",(user['id'],))
        q.execute('SELECT * FROM office_agents WHERE legacy_user_id=%s',(user['id'],))
    a=q.fetchone()
    if not a: raise PermissionError('Agent identity unavailable')
    return dict(a)


def agent_self(user):
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        _,_,me,mine,own=_context(q,user['id']);a=_actor(q,user)
        return dict(agent_id=a['id'],name=a['name'],office=_office(me,mine),legacy=bool(a['legacy_user_id']),
                    peers=[dict(agent_id=x['id'],name=x['name']) for x in _agent_rows(q,own) if x['id']!=a['id'] and not x['revoked']])


def report(user,state,task,summary=''):
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        _context(q,user['id']);a=_actor(q,user)
        q.execute('UPDATE office_agents SET last_seen_at=NOW() WHERE id=%s',(a['id'],))
        q.execute('INSERT INTO office_updates(user_id,agent_id,state,task,summary) VALUES(%s,%s,%s,%s,%s) RETURNING id,agent_id,state,task,summary,created_at',
                  (user['id'],a['id'],state,task.strip(),summary.strip()))
        return dict(q.fetchone())


def heartbeat(user):
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        _context(q,user['id']);a=_actor(q,user)
        q.execute('UPDATE office_agents SET last_seen_at=NOW() WHERE id=%s RETURNING last_seen_at',(a['id'],))
        return dict(agent_id=a['id'],last_seen_at=q.fetchone()['last_seen_at'])


def update_agent(user,agent_id,name,animal,cloth):
    if animal not in accounts.AVATAR_ANIMALS or cloth not in accounts.AVATAR_CLOTHS:
        raise ValueError('Unknown avatar')
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        _context(q,user['id'])
        # Machine callers may edit only their own identity, never a sibling agent.
        if user.get('_agent_token_id') and _actor(q,user)['id']!=agent_id:
            raise PermissionError('Only your own agent can be edited')
        q.execute('UPDATE office_agents SET name=%s,animal=%s,cloth=%s WHERE id=%s AND user_id=%s RETURNING id',
                  (name.strip(),animal,cloth,agent_id,user['id']))
        if not q.fetchone():raise PermissionError('Only your own agent can be edited')
        q.execute('UPDATE agent_tokens SET label=%s WHERE id=(SELECT token_id FROM office_agents WHERE id=%s)',(name.strip(),agent_id))
        return dict(ok=True,agent_id=agent_id)


def send(user,recipient_id,kind,subject,body,client_id):
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        _,_,_,mine,own=_context(q,user['id']);a=_actor(q,user)
        peers={p['id']:p for p in _agent_rows(q,own) if not p['revoked']}
        if recipient_id not in peers or recipient_id==a['id']:
            raise PermissionError('Recipient must be another active identity in this office')
        q.execute('SELECT * FROM agent_messages WHERE sender_id=%s AND client_id=%s',(a['id'],client_id))
        prior=q.fetchone()
        if prior:
            if prior['office_id']!=mine['id']:
                raise ValueError('client_id belongs to a previous office')
            if (prior['recipient_id'],prior['kind'],prior['subject'],prior['body'])!=(recipient_id,kind,subject,body):
                raise ValueError('client_id already used for a different message')
            return dict(prior)
        q.execute('''INSERT INTO agent_messages(office_id,sender_id,recipient_id,kind,subject,body,client_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(mine['id'],a['id'],recipient_id,kind,subject,body,client_id))
        return dict(q.fetchone())


def messages(user,agent_only=True,pending_only=False):
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        _,_,_,mine,own=_context(q,user['id'])
        a=_actor(q,user) if agent_only else None
        # Recheck both endpoints' current office so transfers cannot expose old conversations.
        peers=[p['id'] for p in _agent_rows(q,own) if not p['revoked']]
        q.execute('''SELECT m.*,s.name AS sender_name,r.name AS recipient_name FROM agent_messages m
            JOIN office_agents s ON s.id=m.sender_id JOIN office_agents r ON r.id=m.recipient_id
            WHERE m.office_id=%s AND m.sender_id=ANY(%s) AND m.recipient_id=ANY(%s)
            AND (%s::bigint IS NULL OR m.recipient_id=%s OR m.sender_id=%s)
            AND (NOT %s OR (m.state='pending' AND m.recipient_id=%s))
            ORDER BY CASE WHEN %s THEN m.id END ASC, m.updated_at DESC,m.id DESC LIMIT 100''',
            (mine['id'],peers,peers,a['id'] if a else None,a['id'] if a else None,a['id'] if a else None,
             pending_only,a['id'] if a else None,pending_only))
        return dict(messages=_rows(q),agent_id=a['id'] if a else None)


def acknowledge(user,message_id,state):
    ensure_schema()
    with accounts._db() as c,c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as q:
        _,_,_,mine,own=_context(q,user['id']);a=_actor(q,user)
        peers=[p['id'] for p in _agent_rows(q,own) if not p['revoked']]
        q.execute('SELECT * FROM agent_messages WHERE id=%s AND office_id=%s AND recipient_id=%s AND sender_id=ANY(%s) FOR UPDATE',
                  (message_id,mine['id'],a['id'],peers))
        m=q.fetchone()
        if not m:raise PermissionError('Only the recipient can acknowledge this message')
        transitions={'pending':{'read'} if m['kind']=='message' else {'accepted','declined'},'accepted':{'completed'}}
        if state!=m['state'] and state not in transitions.get(m['state'],set()):
            raise ValueError('Invalid handoff transition')
        q.execute('UPDATE agent_messages SET state=%s,updated_at=NOW() WHERE id=%s RETURNING *',(state,message_id))
        return dict(q.fetchone())
