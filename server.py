#!/usr/bin/env python3
"""Abkhaz Air: static site + private SQLite request storage. Python 3.10+; no packages."""
from __future__ import annotations
import argparse
import base64
from collections import defaultdict, deque
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
MAX_BODY = 32768
USERNAME = re.compile(r'(?=.{3,20}$)[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?\Z', re.ASCII)
DISCORD = re.compile(r'(?!.*\.\.)[A-Za-z0-9_.]{2,32}\Z', re.ASCII)
LEGACY_DISCORD = re.compile(r'[^\s<>\x00-\x1f]{2,32}#[0-9]{4}\Z')
PRICES = {'economy': 0, 'business': 75}

class APIError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def private_key(path):
    """Never replace an existing key: old receipts must remain readable after restart."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding='utf-8').strip()
    value = secrets.token_urlsafe(48)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(value + '\n')
        f.flush()
        os.fsync(f.fileno())
    return value

class Store:
    def __init__(self, data_dir, flights_path=ROOT / 'flights.json'):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.data_dir / 'requests.sqlite3'
        self.master_key = private_key(self.data_dir / 'receipt.key').encode()
        self.admin_key = os.environ.get('ABKHAZ_ADMIN_KEY') or private_key(self.data_dir / 'admin.key')
        if len(self.admin_key) < 24:
            raise ValueError('ABKHAZ_ADMIN_KEY must contain at least 24 characters.')
        self.flights_path = Path(flights_path)
        with closing(self.connect()) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS bookings (
              reference TEXT PRIMARY KEY,
              idempotency_key TEXT UNIQUE NOT NULL,
              payload_hash TEXT NOT NULL,
              secret_hash TEXT NOT NULL,
              flight_json TEXT NOT NULL,
              cabin TEXT NOT NULL CHECK(cabin IN ('economy','business')),
              passengers_json TEXT NOT NULL,
              total_robux INTEGER NOT NULL CHECK(total_robux >= 0),
              created_at TEXT NOT NULL,
              language TEXT NOT NULL,
              status TEXT NOT NULL
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS bookings_created ON bookings(created_at DESC)')
            db.commit()
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def flights(self):
        flights = json.loads(self.flights_path.read_text(encoding='utf-8'))
        if not isinstance(flights, list):
            raise ValueError('flights.json must be an array')
        return flights

    def secret_for(self, reference):
        raw = hmac.new(self.master_key, reference.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')

    def public_booking(self, row, include_secret=False):
        flight = json.loads(row['flight_json'])
        current_flight = next((item for item in self.flights() if item['id'] == flight['id']), None)
        if current_flight:
            flight = {**flight, **current_flight}
        value = {
            'reference': row['reference'], 'flight': flight,
            'cabin': row['cabin'], 'passengers': json.loads(row['passengers_json']),
            'total_robux': row['total_robux'], 'created_at': row['created_at'],
            'language': row['language'], 'status': row['status']
        }
        if include_secret:
            value['secret'] = self.secret_for(row['reference'])
        return value

    def validate(self, data):
        if not isinstance(data, dict):
            raise APIError('invalid_request')
        key = data.get('idempotency_key')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{24,80}', key):
            raise APIError('invalid_request')
        flight = next((f for f in self.flights() if f['id'] == data.get('flight_id')), None)
        if not flight:
            raise APIError('flight_unavailable')
        cabin = data.get('cabin')
        if not isinstance(cabin, str) or cabin not in PRICES:
            raise APIError('cabin_invalid')
        if data.get('consent') is not True:
            raise APIError('consent_required')
        passengers = data.get('passengers')
        if not isinstance(passengers, list) or not 1 <= len(passengers) <= 4:
            raise APIError('too_many_passengers')
        clean, seen = [], set()
        for passenger in passengers:
            if not isinstance(passenger, dict):
                raise APIError('invalid_request')
            roblox, discord = passenger.get('roblox'), passenger.get('discord')
            if not isinstance(roblox, str) or not USERNAME.fullmatch(roblox.strip()):
                raise APIError('invalid_username')
            roblox = roblox.strip()
            if not isinstance(discord, str):
                raise APIError('invalid_discord')
            discord = discord.strip().removeprefix('@')
            if not (DISCORD.fullmatch(discord) or LEGACY_DISCORD.fullmatch(discord)):
                raise APIError('invalid_discord')
            if roblox.lower() in seen:
                raise APIError('duplicate_passenger')
            seen.add(roblox.lower())
            clean.append({'roblox': roblox, 'discord': discord})
        language = data.get('language', 'ru')
        if language not in ('ru', 'en', 'ab'):
            language = 'ru'
        payload = {'flight_id': flight['id'], 'cabin': cabin, 'passengers': clean, 'consent': True}
        return key, flight, cabin, clean, language, digest(canonical(payload))

    def create_booking(self, data):
        key, flight, cabin, passengers, language, payload_hash = self.validate(data)
        with closing(self.connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT * FROM bookings WHERE idempotency_key=?', (key,)).fetchone()
                if existing:
                    if not hmac.compare_digest(existing['payload_hash'], payload_hash):
                        raise APIError('conflict', 409)
                    db.commit()
                    return self.public_booking(existing, True), False
                reference = 'AA-' + secrets.token_hex(6).upper()
                created = datetime.now(timezone.utc).isoformat(timespec='seconds')
                db.execute('''INSERT INTO bookings
                    (reference,idempotency_key,payload_hash,secret_hash,flight_json,cabin,
                    passengers_json,total_robux,created_at,language,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (
                    reference, key, payload_hash, digest(self.secret_for(reference)), canonical(flight),
                    cabin, canonical(passengers), PRICES[cabin]*len(passengers), created, language,
                    'demo_received' if flight.get('demo') else 'request_received'))
                row = db.execute('SELECT * FROM bookings WHERE reference=?', (reference,)).fetchone()
                db.commit()  # The browser never sees success before the durable commit.
                return self.public_booking(row, True), True
            except BaseException:
                db.rollback()
                raise

    def lookup(self, reference, secret):
        if not isinstance(reference, str) or not isinstance(secret, str) or len(reference)>40 or len(secret)>120:
            raise APIError('not_found', 404)
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM bookings WHERE reference=?', (reference.strip().upper(),)).fetchone()
        valid = hmac.compare_digest(row['secret_hash'] if row else '0'*64, digest(secret.strip()))
        if row is None or not valid:
            raise APIError('not_found', 404)
        return self.public_booking(row)

    def mine(self, data):
        credentials = data.get('credentials')
        if not isinstance(credentials, list) or len(credentials)>100:
            raise APIError('invalid_request')
        items, seen = [], set()
        for item in credentials:
            if not isinstance(item, dict):
                continue
            try:
                booking = self.lookup(item.get('reference'), item.get('secret'))
            except APIError:
                continue
            if booking['reference'] not in seen:
                items.append(booking)
                seen.add(booking['reference'])
        return sorted(items, key=lambda x:x['created_at'], reverse=True)

    def admin_list(self, query='', offset=0, limit=50):
        query = query[:100]
        like = '%' + query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
        where = "WHERE reference LIKE ? ESCAPE '\\' OR passengers_json LIKE ? ESCAPE '\\'"
        with closing(self.connect()) as db:
            total = db.execute('SELECT count(*) FROM bookings').fetchone()[0]
            filtered = db.execute('SELECT count(*) FROM bookings '+where, (like,like)).fetchone()[0]
            rows = db.execute('SELECT * FROM bookings '+where+' ORDER BY created_at DESC,reference LIMIT ? OFFSET ?',
                              (like,like,limit,offset)).fetchall()
        return {'bookings':[self.public_booking(row) for row in rows], 'total':total,
                'filtered':filtered, 'offset':offset, 'limit':limit, 'has_more':offset+limit<filtered}

    def export_csv(self):
        output=io.StringIO(newline='')
        writer=csv.writer(output)
        writer.writerow(['Request','Created UTC','Flight','Date','Departure MSK','Arrival MSK','Aircraft','Class','Passengers','Total Robux','Roblox','Discord','Status','Language'])
        def cell(value):
            value=str(value)
            return "'"+value if value.startswith(('=','+','-','@','\t','\r')) else value
        with closing(self.connect()) as db:
            for row in db.execute('SELECT * FROM bookings ORDER BY created_at DESC,reference'):
                b=self.public_booking(row);f=b['flight']
                for p in b['passengers']:
                    writer.writerow([cell(v) for v in [b['reference'],b['created_at'],f["origin"]+' → '+f['destination'],f['date'],f['departure'],f['arrival'],f['aircraft'],b['cabin'],len(b['passengers']),b['total_robux'],p['roblox'],p['discord'],b['status'],b['language']]])
        return ('\ufeff'+output.getvalue()).encode('utf-8')

class RateLimiter:
    def __init__(self):
        self.events=defaultdict(deque)
        self.lock=threading.Lock()
    def allow(self, bucket, limit=40):
        now=time.monotonic()
        with self.lock:
            if len(self.events)>5000:
                self.events=defaultdict(deque,{k:v for k,v in self.events.items() if v and v[-1]>now-60})
            queue=self.events[bucket]
            while queue and queue[0]<now-60:
                queue.popleft()
            if len(queue)>=limit:
                return False
            queue.append(now)
            return True

class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,store):
        super().__init__(address,Handler)
        self.store=store
        self.limiter=RateLimiter()

class Handler(BaseHTTPRequestHandler):
    server_version='AbkhazAir/1.0'
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
        self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()')
        super().end_headers()

    def send(self,status,body,content_type='application/json; charset=utf-8',extra=None):
        if isinstance(body, (dict,list)):
            body=canonical(body).encode('utf-8')
        elif isinstance(body,str):
            body=body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        for k,v in (extra or {}).items():
            self.send_header(k,v)
        self.end_headers()
        if self.command!='HEAD':
            self.wfile.write(body)

    def rate_limit(self,bucket,limit=40):
        if not self.server.limiter.allow((self.client_address[0],bucket),limit):
            raise APIError('too_many_requests',429)

    def require_admin(self):
        self.rate_limit('admin',120)
        header=self.headers.get('Authorization','')
        key=header.removeprefix('Bearer ') if header.startswith('Bearer ') else ''
        if not hmac.compare_digest(key.encode(),self.server.store.admin_key.encode()):
            raise APIError('unauthorized',401)

    def read_json(self):
        origin=self.headers.get('Origin')
        expected=os.environ.get('ABKHAZ_PUBLIC_ORIGIN','').rstrip('/')
        if origin and origin!='null' and origin!=expected and urlsplit(origin).netloc!=self.headers.get('Host'):
            raise APIError('unauthorized',403)
        if origin=='null':
            raise APIError('unauthorized',403)
        if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
            raise APIError('invalid_request',415)
        try:
            size=int(self.headers.get('Content-Length','0'))
        except ValueError:
            raise APIError('invalid_request')
        if not 0<size<=MAX_BODY:
            raise APIError('invalid_request',413)
        try:
            value=json.loads(self.rfile.read(size))
        except (UnicodeDecodeError,json.JSONDecodeError):
            raise APIError('invalid_request')
        if not isinstance(value,dict):
            raise APIError('invalid_request')
        return value

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            url=urlsplit(self.path)
            path=unquote(url.path)
            if path=='/api/flights':
                return self.send(200,{'flights':self.server.store.flights()})
            if path=='/api/health':
                return self.send(200,{'ok':True})
            if path=='/api/admin/bookings':
                self.require_admin();q=parse_qs(url.query)
                try:
                    offset=max(0,int(q.get('offset',['0'])[0]))
                except ValueError:
                    raise APIError('invalid_request')
                return self.send(200,self.server.store.admin_list(q.get('q',[''])[0],offset))
            if path=='/api/admin/export':
                self.require_admin()
                return self.send(200,self.server.store.export_csv(),'text/csv; charset=utf-8',{'Content-Disposition':'attachment; filename="abkhaz-air-requests.csv"'})
            if path.startswith('/api/'):
                raise APIError('not_found',404)
            if path=='/': path='/index.html'
            if path=='/admin':path='/admin.html'
            public=(ROOT/'public').resolve()
            target=(public/path.lstrip('/')).resolve()
            if not target.is_relative_to(public) or not target.is_file() or target.suffix not in ('.html','.js','.css','.png','.svg','.webp','.ico'):
                return self.send(404,'Not found','text/plain; charset=utf-8')
            content_type=mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            if target.suffix=='.js':content_type='application/javascript'
            if target.suffix in ('.html','.js','.css','.svg'):content_type+='; charset=utf-8'
            return self.send(200,target.read_bytes(),content_type)
        except APIError as error:
            self.send(error.status,{'error':error.code})
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception as error:
            self.log_error('Request failed: %s',type(error).__name__)
            self.send(500,{'error':'server_error'})

    def do_POST(self):
        try:
            path=urlsplit(self.path).path
            self.rate_limit('write',40)
            data=self.read_json()
            if path=='/api/bookings':
                booking,created=self.server.store.create_booking(data)
                return self.send(201 if created else 200,booking)
            if path=='/api/bookings/mine':
                return self.send(200,{'bookings':self.server.store.mine(data)})
            if path=='/api/bookings/lookup':
                return self.send(200,self.server.store.lookup(data.get('reference'),data.get('secret')))
            raise APIError('not_found',404)
        except APIError as error:
            self.send(error.status,{'error':error.code})
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception as error:
            self.log_error('Request failed: %s',type(error).__name__)
            self.send(500,{'error':'server_error'})

    def log_message(self,fmt,*args):
        # No request bodies, private keys, nicknames or authorization headers are logged.
        message=fmt % args
        if '?' in message:
            message=message.split('?')[0]+' [query omitted]'
        print(f'[{self.log_date_time_string()}] {message}',flush=True)


def main():
    parser=argparse.ArgumentParser(description='Abkhaz Air website with SQLite request storage')
    parser.add_argument('--open',action='store_true',help='Open the site in your browser')
    parser.add_argument('--host',default=os.environ.get('HOST','127.0.0.1'))
    parser.add_argument('--port',default=int(os.environ.get('PORT','8080')),type=int)
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('ABKHAZ_DATA_DIR',ROOT/'data')))
    args=parser.parse_args()
    store=Store(args.data_dir)
    server=Server((args.host,args.port),store)
    print(f'Abkhaz Air: http://localhost:{args.port}',flush=True)
    print(f'Team page: http://localhost:{args.port}/admin.html',flush=True)
    print('The team access key is in data/admin.key (or ABKHAZ_ADMIN_KEY). Keep the data folder.',flush=True)
    if args.open:
        import webbrowser
        threading.Timer(.5,lambda:webbrowser.open(f'http://localhost:{args.port}')).start()
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        print('\nServer stopped. Requests remain in the database.',flush=True)
    finally:
        server.server_close()

if __name__=='__main__':
    main()
