"""Run seed on mod-v0.1.6 and verify on the new image with an isolated volume.

Uses ordinary X11 input and local HTTP/STUN/proxy fixtures, never native CDP.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import httpx

BASE = 'http://127.0.0.1:8080/api'
ORIGIN = 'http://127.0.0.1:18190'
CROSS = 'http://127.0.0.1:18191'
PROXY = 'http://127.0.0.1:18192'
STUN_HOST = socket.gethostbyname(socket.gethostname())
SNAPSHOT = Path('/data/e2e-webrtc-legacy.json')
reports = {}
stun_packets = 0
proxy_requests = 0


def api(method, path, data=None):
    r = httpx.request(method, BASE + path, json=data, timeout=120, trust_env=False)
    r.raise_for_status()
    return r.json()


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        key = parse_qs(urlsplit(self.path).query)['key'][0]
        reports[key] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlsplit(self.path)
        q = parse_qs(parsed.query)
        key = q.get('key', ['unknown'])[0]
        if parsed.path == '/probe.js':
            body = self.probe(q)
            content_type = 'text/javascript'
        elif parsed.path == '/frame.js':
            body = '''const state = {available: typeof RTCPeerConnection === 'function', constructed: false};
            try { const pc = new RTCPeerConnection(); state.constructed = true; pc.close(); } catch {}
            fetch('/report?key=' + KEY, {method:'POST',body:JSON.stringify(state)});'''.replace('KEY', json.dumps(key))
            content_type = 'text/javascript'
        else:
            script = '/frame.js' if parsed.path == '/frame' else '/probe.js'
            body = '<!doctype html><title>WebRTC E2E</title><script src="' + script + '?' + parsed.query.replace('&', '&amp;') + '"></script><body>WebRTC E2E</body>'
            content_type = 'text/html'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        if q.get('csp') == ['1']:
            self.send_header('Content-Security-Policy', "script-src 'self'; connect-src 'self'; frame-src 'self' " + CROSS)
        self.end_headers()
        self.wfile.write(body.encode())

    @staticmethod
    def probe(q):
        config = {'key': q['key'][0], 'persist': q['persist'][0], 'mode': q['mode'][0], 'origin': ORIGIN, 'cross': CROSS, 'stun': f'stun:{STUN_HOST}:18193'}
        return r'''const config = CONFIG;
        const state = {available: typeof RTCPeerConnection === 'function', constructed: false,
          webdriver: navigator.webdriver, cookie: document.cookie, storage: localStorage.getItem('webrtc-e2e'), candidates: [], dataChannel: false};
        document.cookie = 'webrtc-e2e=' + config.persist + '; Max-Age=86400; Path=/';
        localStorage.setItem('webrtc-e2e', config.persist);
        // All frames run an external script as their first page script.
        function frames() {
          for (const [name, origin] of [['same', config.origin], ['cross', config.cross]]) {
            const frame = document.createElement('iframe');
            frame.src = origin + '/frame?key=' + config.key + '-' + name;
            document.documentElement.append(frame);
          }
          const blank = document.createElement('iframe');
          document.documentElement.append(blank);
          blank.contentDocument.write('<script src="' + config.origin + '/frame.js?key=' + config.key + '-blank"><\/script>');
          blank.contentDocument.close();
          const srcdoc = document.createElement('iframe');
          srcdoc.srcdoc = '<script src="' + config.origin + '/frame.js?key=' + config.key + '-srcdoc"><\/script>';
          document.documentElement.append(srcdoc);
        }
        async function pair() {
          let a, b;
          try {
            a = new RTCPeerConnection(); b = new RTCPeerConnection();
            a.onicecandidate = e => { if(e.candidate) b.addIceCandidate(e.candidate).catch(()=>{}); };
            b.onicecandidate = e => { if(e.candidate) a.addIceCandidate(e.candidate).catch(()=>{}); };
            const done = new Promise(resolve => { b.ondatachannel = e => { e.channel.onmessage = m => resolve(m.data === 'roundtrip'); }; });
            const channel = a.createDataChannel('e2e'); channel.onopen = () => channel.send('roundtrip');
            await a.setLocalDescription(await a.createOffer()); await b.setRemoteDescription(a.localDescription);
            await b.setLocalDescription(await b.createAnswer()); await a.setRemoteDescription(b.localDescription);
            return await Promise.race([done, new Promise(resolve=>setTimeout(()=>resolve(false),6000))]);
          } finally { a?.close(); b?.close(); }
        }
        async function run() {
          frames();
          let pc;
          try {
            pc = new RTCPeerConnection({iceServers:[{urls:config.stun}]}); state.constructed = true;
            pc.onicecandidate = e => { if(e.candidate) state.candidates.push(e.candidate.toJSON()); };
            pc.createDataChannel('probe'); await pc.setLocalDescription(await pc.createOffer());
            await new Promise(resolve=>setTimeout(resolve,4000));
            if(config.mode === 'enabled') state.dataChannel = await pair();
          } catch(e) { state.error = String(e); }
          finally { pc?.close(); }
          await fetch('/report?key='+config.key,{method:'POST',body:JSON.stringify(state)});
        }
        run();'''.replace('CONFIG', json.dumps(config))


class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def forward(self):
        global proxy_requests
        url = urlsplit(self.path)
        if url.hostname not in {'127.0.0.1', 'localhost', STUN_HOST}:
            self.send_error(403)
            return
        proxy_requests += 1
        body = self.rfile.read(int(self.headers.get('Content-Length', '0'))) if self.command == 'POST' else None
        headers = {k:v for k,v in self.headers.items() if k.lower() not in {'host', 'proxy-connection', 'connection', 'content-length'}}
        try:
            with build_opener(ProxyHandler({})).open(Request(self.path, data=body, headers=headers, method=self.command), timeout=10) as r:
                self.send_response(r.status)
                for k,v in r.headers.items():
                    if k.lower() not in {'connection','transfer-encoding','content-length'}:
                        self.send_header(k,v)
                self.end_headers()
                self.wfile.write(r.read())
        except Exception:
            self.send_error(502)

    do_GET = forward
    do_POST = forward


def stun_loop(sock):
    global stun_packets
    while True:
        try:
            packet, address = sock.recvfrom(65535)
        except OSError:
            return
        if len(packet) < 20 or packet[:2] != b'\x00\x01' or packet[4:8] != b'\x21\x12\xa4\x42':
            continue
        stun_packets += 1
        # A deterministic public documentation address proves srflx gathering
        # without sending browser traffic to an Internet STUN service.
        mapped = struct.pack('!BBHI',0,1,address[1] ^ 0x2112,int(ipaddress.IPv4Address('198.51.100.77')) ^ 0x2112a442)
        attr = struct.pack('!HH',0x0020,8) + mapped
        sock.sendto(struct.pack('!HHI',0x0101,len(attr),0x2112a442) + packet[8:20] + attr,address)


async def get_report(key):
    for _ in range(150):
        if key in reports:
            return reports.pop(key)
        await asyncio.sleep(0.1)
    raise AssertionError('No report: ' + key)


async def probe(profile, expected, *, persist=False, proxy=False, csp=False):
    global stun_packets, proxy_requests
    key = uuid.uuid4().hex
    url = ORIGIN + f'/probe?key={key}&persist={profile["browser_type"]}&mode={expected}&csp={int(csp)}'
    args = [url] if profile['browser_type'] != 'camoufox' else []
    if proxy and profile['browser_type'] == 'chromium':
        args.insert(0,'--proxy-bypass-list=<-loopback>')
    api('PUT', '/profiles/'+profile['id'], {'launch_args':args, 'proxy':PROXY if proxy else None})
    packets_before, requests_before = stun_packets, proxy_requests
    launch = api('POST','/profiles/'+profile['id']+'/launch')
    try:
        assert launch['cdp_url'] is None
        if profile['browser_type'] == 'camoufox':
            env = {**os.environ,'DISPLAY':launch['display']}
            subprocess.run(['xdotool','key','ctrl+l'],env=env,check=True)
            subprocess.run(['xdotool','type','--clearmodifiers',url],env=env,check=True)
            subprocess.run(['xdotool','key','Return'],env=env,check=True)
        state = await get_report(key)
        assert state['webdriver'] is False, state
        assert state['available'] == (expected != 'disabled'), state
        assert state['constructed'] == (expected != 'disabled'), state
        for suffix in ('same','cross','blank','srcdoc'):
            frame = await get_report(key+'-'+suffix)
            assert frame['available'] == (expected != 'disabled'), (suffix, frame)
            assert frame['constructed'] == (expected != 'disabled'), (suffix, frame)
        if persist:
            assert state['storage'] == profile['browser_type'], state
            assert 'webrtc-e2e='+profile['browser_type'] in state['cookie'], state
        if expected in {'disabled','protect_ip'}:
            assert stun_packets == packets_before, (expected,stun_packets-packets_before,state)
            assert not state['candidates'], state
        else:
            assert stun_packets > packets_before, state
            assert state['dataChannel'], state
        if proxy:
            assert proxy_requests > requests_before, 'Proxy fixture was not used'
        return state
    finally:
        api('POST','/profiles/'+profile['id']+'/stop')


async def seed():
    profiles=[]
    for kind in ('chromium','firefox','camoufox'):
        p=api('POST','/profiles',{'name':kind+' legacy WebRTC','browser_type':kind,'screen_width':1280,'screen_height':720,'platform':'linux','firefox_preferences':{'network.proxy.allow_hijacking_localhost':True}})
        await probe(p,'enabled')
        profiles.append({'profile':api('GET','/profiles/'+p['id']),'kdbx_sha256':hashlib.sha256((Path(p['user_data_dir'])/'KeePassXC/passwords.kdbx').read_bytes()).hexdigest()})
        print('PASS: seeded legacy '+kind+' WebRTC, cookies, storage and KeePass',flush=True)
    SNAPSHOT.write_text(json.dumps(profiles))


async def verify():
    for saved in json.loads(SNAPSHOT.read_text()):
        old=saved['profile']; p=api('GET','/profiles/'+old['id'])
        assert p['webrtc_mode']=='enabled'
        assert not p['remote_debugging_enabled'] and not p['playwright_enabled']
        for field in old:
            if field not in {'status','vnc_ws_port','cdp_url'}: assert p[field]==old[field],field
        assert hashlib.sha256((Path(p['user_data_dir'])/'KeePassXC/passwords.kdbx').read_bytes()).hexdigest()==saved['kdbx_sha256']
        await probe(p,'enabled',persist=True)
        print('PASS: legacy '+p['browser_type']+' settings/data/WebRTC preserved',flush=True)
    for kind in ('chromium','firefox','camoufox'):
        p=api('POST','/profiles',{'name':kind+' WebRTC modes','browser_type':kind,'screen_width':1280,'screen_height':720,'platform':'linux','firefox_preferences':{'network.proxy.allow_hijacking_localhost':True}})
        assert p['webrtc_mode']=='disabled'
        fingerprint=None; kdbx=None
        for index,mode in enumerate(('disabled','protect_ip','enabled','disabled','enabled')):
            p=api('PUT','/profiles/'+p['id'],{'webrtc_mode':mode})
            await probe(p,mode,persist=index>0,proxy=mode=='protect_ip',csp=True)
            path=Path(p['user_data_dir'])
            value=hashlib.sha256((path/'KeePassXC/passwords.kdbx').read_bytes()).hexdigest()
            if kdbx: assert value==kdbx
            kdbx=value
            if kind=='camoufox':
                value=json.loads((path/'camoufox-fingerprint.json').read_text())
                if fingerprint: assert value==fingerprint
                fingerprint=value
            print('PASS: '+kind+' '+mode+' frames/CSP/ICE/persistence',flush=True)
        update={'webrtc_mode':'disabled'}
        if kind=='chromium': update['launch_args']=['--disable-extensions']
        else: update['firefox_preferences']={'media.peerconnection.enabled':True,'network.proxy.allow_hijacking_localhost':True}
        p=api('PUT','/profiles/'+p['id'],update)
        # Preserve the explicit native argument when constructing this probe.
        if kind=='chromium':
            await advanced_chromium(p)
        else:
            await probe(p,'enabled',persist=True)
        print('PASS: '+kind+' advanced override',flush=True)
    await verify_ui()


async def advanced_chromium(profile):
    # Test the recognized extension-disable switch with the same local fixture.
    key=uuid.uuid4().hex
    url=ORIGIN+f'/probe?key={key}&persist=chromium&mode=protect_ip'
    api('PUT','/profiles/'+profile['id'],{'launch_args':['--disable-extensions',url],'proxy':None})
    launch=api('POST','/profiles/'+profile['id']+'/launch')
    try:
        state=await get_report(key)
        assert state['available'] and state['constructed'] and state['webdriver'] is False,state
    finally:
        api('POST','/profiles/'+profile['id']+'/stop')


async def verify_ui():
    from camoufox.async_api import AsyncCamoufox
    async with AsyncCamoufox(headless=True) as browser:
        page=await browser.new_page()
        await page.goto(BASE.removesuffix('/api'))
        for kind in ('chromium','firefox','camoufox'):
            await page.get_by_role('button',name='New Profile').click()
            await page.get_by_label('Browser Type').select_option(kind)
            assert await page.get_by_label('WebRTC',exact=True).input_value()=='disabled'
            await page.get_by_placeholder('e.g. Amazon Seller #1').fill(kind+' WebRTC UI')
            await page.get_by_role('button',name='Create',exact=True).click()
            await page.get_by_role('heading',name='Edit Profile',exact=True).wait_for()
            for mode in ('protect_ip','enabled','disabled'):
                await page.get_by_label('WebRTC',exact=True).select_option(mode)
                await page.get_by_role('button',name='Save',exact=True).click()
                await page.get_by_role('button',name='Save',exact=True).wait_for(state='visible')
                assert await page.get_by_label('WebRTC',exact=True).input_value()==mode
            await page.get_by_role('button',name='Cancel',exact=True).click()
        await page.get_by_role('button',name='New Profile').click()
        assert await page.get_by_label('WebRTC',exact=True).count()==0
        print('PASS: real React WebRTC default, edits and CloakBrowser scope',flush=True)


async def main():
    for _ in range(60):
        try:
            api('GET', '/status')
            break
        except httpx.ConnectError:
            await asyncio.sleep(0.25)
    else:
        raise RuntimeError('Test container API did not become ready')
    servers=[]
    for port,handler in ((18190,Fixture),(18191,Fixture),(18192,Proxy)):
        s=ThreadingHTTPServer(('127.0.0.1',port),handler)
        threading.Thread(target=s.serve_forever,daemon=True).start(); servers.append(s)
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0',18193))
    threading.Thread(target=stun_loop,args=(sock,),daemon=True).start()
    try:
        await (seed() if sys.argv[1]=='seed' else verify())
    finally:
        sock.close()
        for server in servers: server.shutdown()


if __name__=='__main__':
    asyncio.run(main())
