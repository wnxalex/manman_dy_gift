# -*- coding: utf-8 -*-
"""
抖音直播间礼物点歌机 · 带网页控制台
- 浏览器里配置：礼物→歌曲、上传歌曲、BGM 背景音乐列表、音量等
- 收到指定礼物 -> 排队播放对应歌曲；空闲时循环播放 BGM；礼物歌优先，播完自动恢复 BGM
基于 DouYin_Spider (https://github.com/cv-cat/DouYin_Spider) 的直播间监听能力。仅供学习与个人使用。
"""
import gzip
import json
import os
import queue
import random
import sys
import threading
import time
import webbrowser
from collections import deque
from urllib.parse import urlencode

from flask import Flask, request, jsonify, send_from_directory

# ---------- 路径处理：打包后 exe 旁边找资源，捆绑的 node 加入 PATH ----------
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))   # 可执行文件所在目录（放 config/songs/bgm）
    RES_DIR = sys._MEIPASS                                        # 解包后的只读资源（webui/static/node_modules）
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_DIR

os.environ['PATH'] = APP_DIR + os.pathsep + os.path.join(APP_DIR, 'node') + os.pathsep + os.environ.get('PATH', '')

CONFIG_PATH = os.path.join(APP_DIR, 'config.json')
SONGS_DIR = os.path.join(APP_DIR, 'songs')
BGM_DIR = os.path.join(APP_DIR, 'bgm')
WEBUI_DIR = os.path.join(RES_DIR, 'webui')
ALLOWED_EXT = {'.mp3', '.wav', '.ogg', '.flac', '.m4a'}

for d in (SONGS_DIR, BGM_DIR):
    os.makedirs(d, exist_ok=True)

DEFAULT_CONFIG = {
    "live_id": "",
    "cookie": "",
    "gift_songs": {},          # {"玫瑰": "songs/a.mp3"}
    "bgm_list": [],            # ["bgm/1.mp3", "bgm/2.mp3"]
    "bgm_enabled": True,
    "bgm_shuffle": False,
    "combo_window_seconds": 8,
    "max_queue": 5,
    "volume": 0.9,             # 礼物歌音量
    "bgm_volume": 0.4,         # 背景音乐音量
    "log_all_messages": True
}

# 常见抖音礼物名（下拉候选，用户也可自定义/从直播间实时识别里挑）
COMMON_GIFTS = ["小心心", "玫瑰", "棒棒糖", "仙女棒", "抖音", "为你打call", "比心", "干杯",
                "小啤酒", "大啤酒", "钻石", "跑车", "皇冠", "嘉年华", "礼花筒", "热气球",
                "爱的flowers", "花海", "私人飞机", "城堡", "浪漫烟花", "love", "点赞"]


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def rel_from_app(path):
    """把绝对路径转成相对 APP_DIR 的显示路径（songs/xx.mp3）"""
    try:
        return os.path.relpath(path, APP_DIR).replace('\\', '/')
    except Exception:
        return path


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(APP_DIR, path)


# ==================== 播放引擎 ====================
class Player(threading.Thread):
    """单声道 tick 式播放引擎。
    优先级：试听 > 点歌(礼物歌) > BGM。
    - 点歌歌曲始终完整播放，不受暂停/切歌影响；礼物来了会打断正在放的 BGM。
    - 暂停/切歌 仅作用于 BGM。
    - 点歌队列可查看、单条删除、清空。
    """
    KIND_TAG = {'bgm': 'BGM', 'gift': '点歌', 'test': '试听'}

    def __init__(self, engine):
        super().__init__(daemon=True)
        self.engine = engine
        self._bgm_idx = 0
        # 当前播放 {'path','kind','reason'} 或 None
        self.current = None
        self.now_playing = ''            # 当前曲名（basename），兼容旧状态
        # 点歌队列（用 list 便于查看/删除）
        self._giftq = []
        self._qlock = threading.Lock()
        # BGM 控制
        self.bgm_paused = False
        self._paused_applied = False
        self._skip_bgm = False
        self._bgm_jump = None            # 点选切歌：待切到的指定 BGM 路径
        # 试听
        self._test = None
        self._test_lock = threading.Lock()
        try:
            import pygame
            pygame.mixer.init()
            self.pg = pygame
        except Exception as e:
            self.pg = None
            engine.log(f'[音频] 初始化失败：{e}（BGM/播放不可用）')

    # ---------- 点歌队列 ----------
    def enqueue_gift(self, song_path, reason):
        with self._qlock:
            if len(self._giftq) >= int(self.engine.cfg.get('max_queue', 5)):
                self.engine.log(f'[点歌] 队列已满，忽略：{os.path.basename(song_path)}')
                return
            self._giftq.append((song_path, reason))
            n = len(self._giftq)
        self.engine.log(f'[点歌] {reason} → 入队（排队 {n} 首）')

    def _pop_gift(self):
        with self._qlock:
            return self._giftq.pop(0) if self._giftq else None

    def _giftq_has(self):
        with self._qlock:
            return len(self._giftq) > 0

    def queue_snapshot(self):
        with self._qlock:
            return [{'song': os.path.basename(p), 'reason': r} for p, r in self._giftq]

    def clear_queue(self):
        with self._qlock:
            n = len(self._giftq)
            self._giftq.clear()
        self.engine.log(f'[点歌] 已清空队列（{n} 首）')
        return n

    def remove_queue(self, index):
        with self._qlock:
            if 0 <= index < len(self._giftq):
                p, r = self._giftq.pop(index)
                self.engine.log(f'[点歌] 已移除排队：{os.path.basename(p)}')
                return True
        return False

    # ---------- BGM 控制（仅作用于 BGM） ----------
    def toggle_bgm_pause(self):
        self.bgm_paused = not self.bgm_paused
        self.engine.log('[BGM] ' + ('已暂停' if self.bgm_paused else '已继续'))
        return self.bgm_paused

    def skip_bgm(self):
        self._skip_bgm = True
        self.engine.log('[BGM] 切到下一首')

    def play_bgm(self, path):
        """点选切歌：立即切到指定的 BGM 曲目（点歌/试听播放中则等其结束后再切）。"""
        self._bgm_jump = path
        self.engine.log(f'[BGM] 切到指定曲目：{os.path.basename(path)}')

    def _sync_bgm_idx(self, path):
        """把顺序播放指针对齐到 path 之后，保证之后的自动轮播衔接自然。"""
        lst = [resolve(p) for p in self.engine.cfg.get('bgm_list', [])]
        try:
            self._bgm_idx = lst.index(path) + 1
        except ValueError:
            pass

    # ---------- 试听 ----------
    def play_test(self, path, volume):
        with self._test_lock:
            self._test = (path, float(volume))

    def _pop_test(self):
        with self._test_lock:
            t, self._test = self._test, None
            return t

    def stop_current(self):
        """停止当前试听。"""
        with self._test_lock:
            self._test = None
        if self.current and self.current.get('kind') == 'test':
            try:
                self.pg.mixer.music.stop()
            except Exception:
                pass
            self.current = None
            self.now_playing = ''

    # ---------- 内部：起播 ----------
    def _start(self, path, volume, kind, reason=''):
        pg = self.pg
        if not os.path.exists(path):
            self.engine.log(f'[播放] 文件不存在：{path}')
            self.current = None
            self.now_playing = ''
            return
        try:
            pg.mixer.music.load(path)
            pg.mixer.music.set_volume(float(volume))
            pg.mixer.music.play()
        except Exception as e:
            self.engine.log(f'[播放] 打开失败 {os.path.basename(path)}: {e}')
            self.current = None
            self.now_playing = ''
            return
        self.current = {'path': path, 'kind': kind, 'reason': reason}
        self.now_playing = os.path.basename(path)
        self._paused_applied = False
        tag = self.KIND_TAG.get(kind, '')
        self.engine.log(f'[播放] ♪ {tag} {os.path.basename(path)}' + (f'（{reason}）' if reason else ''))

    def _next_bgm(self):
        cfg = self.engine.cfg
        lst = [resolve(p) for p in cfg.get('bgm_list', []) if os.path.exists(resolve(p))]
        if not lst:
            return None
        if cfg.get('bgm_shuffle'):
            return random.choice(lst)
        self._bgm_idx %= len(lst)
        path = lst[self._bgm_idx]
        self._bgm_idx += 1
        return path

    # ---------- 主循环（tick） ----------
    def run(self):
        if not self.pg:
            return
        while True:
            try:
                self._tick()
            except Exception as e:
                self.engine.log(f'[播放] 循环异常：{e}')
            time.sleep(0.2)

    def _tick(self):
        pg = self.pg
        cfg = self.engine.cfg

        # 1) 试听请求：最高优先级，打断一切
        test = self._pop_test()
        if test:
            try:
                pg.mixer.music.stop()
            except Exception:
                pass
            self._start(test[0], test[1], 'test')
            return

        cur = self.current

        # 2) 判断当前是否自然播完（BGM 暂停时 get_busy 为 False，不算播完）
        if cur:
            paused = (cur['kind'] == 'bgm' and self.bgm_paused)
            if not pg.mixer.music.get_busy() and not paused:
                self.current = None
                self.now_playing = ''
                cur = None

        # 2.5) 点选切歌：立即切到指定 BGM（仅在放 BGM 或空闲时生效；点歌/试听中则保持挂起，等其结束）
        if self._bgm_jump and (cur is None or cur['kind'] == 'bgm'):
            path = self._bgm_jump
            self._bgm_jump = None
            self.bgm_paused = False
            self._paused_applied = False
            try:
                pg.mixer.music.stop()
            except Exception:
                pass
            if os.path.exists(path):
                self._sync_bgm_idx(path)
                self._start(path, cfg.get('bgm_volume', 0.4), 'bgm')
            else:
                self.engine.log(f'[BGM] 文件不存在：{path}')
                self.current = None
                self.now_playing = ''
            return

        # 3) 若正在放 BGM：处理 暂停/继续、切歌、被点歌打断
        if cur and cur['kind'] == 'bgm':
            if self.bgm_paused and not self._paused_applied:
                try:
                    pg.mixer.music.pause()
                except Exception:
                    pass
                self._paused_applied = True
            elif not self.bgm_paused and self._paused_applied:
                try:
                    pg.mixer.music.unpause()
                except Exception:
                    pass
                self._paused_applied = False
            if self._skip_bgm:
                self._skip_bgm = False
                try:
                    pg.mixer.music.stop()
                except Exception:
                    pass
                self.current = None
                self.now_playing = ''
                cur = None
            elif self._giftq_has():
                # 点歌优先，打断 BGM（无论是否暂停）
                try:
                    pg.mixer.music.stop()
                except Exception:
                    pass
                self.current = None
                self.now_playing = ''
                cur = None

        # 切歌标志只对 BGM 有效；当前不是 BGM 时丢弃，避免误触发后续 BGM
        if self._skip_bgm and (not self.current or self.current.get('kind') != 'bgm'):
            self._skip_bgm = False

        # 4) 空闲则挑下一首：点歌优先，其次 BGM（未暂停时）
        if not self.current:
            nxt = self._pop_gift()
            if nxt:
                self._start(nxt[0], cfg.get('volume', 0.9), 'gift', nxt[1])
            elif cfg.get('bgm_enabled') and cfg.get('bgm_list') and not self.bgm_paused:
                p = self._next_bgm()
                if p:
                    self._start(p, cfg.get('bgm_volume', 0.4), 'bgm')

        # 5) 音量热更新（跟随设置实时生效）
        if self.current and not self._paused_applied:
            try:
                if self.current['kind'] == 'gift':
                    pg.mixer.music.set_volume(float(cfg.get('volume', 0.9)))
                elif self.current['kind'] == 'bgm':
                    pg.mixer.music.set_volume(float(cfg.get('bgm_volume', 0.4)))
            except Exception:
                pass


# ==================== 引擎（监听 + 状态） ====================
class Engine:
    def __init__(self):
        self.cfg = load_config()
        self._logs = deque(maxlen=400)
        self._log_lock = threading.Lock()
        self.detected = {}                 # 直播间实时识别到的礼物 name -> {count,last}
        self.running = False
        self.status_msg = '未启动'
        self._ws = None
        self._combo_seen = {}
        self._combo_lock = threading.Lock()
        self.player = Player(self)
        self.player.start()

    # ---- 日志 ----
    def log(self, msg):
        line = f'{time.strftime("%H:%M:%S")} {msg}'
        with self._log_lock:
            self._logs.append(line)
        print(line, flush=True)

    def logs(self):
        with self._log_lock:
            return list(self._logs)

    # ---- 连击去重 ----
    def _should_trigger(self, user_id, gift_id):
        now = time.monotonic()
        window = float(self.cfg.get('combo_window_seconds', 8))
        key = (user_id, gift_id)
        with self._combo_lock:
            last = self._combo_seen.get(key)
            self._combo_seen[key] = now
            if len(self._combo_seen) > 3000:
                self._combo_seen = {k: v for k, v in self._combo_seen.items() if now - v < window}
        return last is None or (now - last) > window

    # ---- 启停 ----
    def start(self):
        if self.running:
            return False, '已经在运行中'
        import shutil
        if shutil.which('node') is None:
            return False, '未检测到 Node.js，请先安装 Node.js LTS 版'
        if not str(self.cfg.get('live_id', '')).strip().isdigit():
            return False, 'live_id 未填写或不是纯数字'
        if len(self.cfg.get('cookie', '')) < 50:
            return False, 'cookie 未填写或不完整'
        self.running = True
        self.status_msg = '正在连接...'
        threading.Thread(target=self._run_listener, daemon=True).start()
        return True, '已启动'

    def stop(self):
        self.running = False
        self.status_msg = '已停止'
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self.log('[控制] 已停止监听')
        return True, '已停止'

    def _run_listener(self):
        self.log('[控制] 开始监听...')
        while self.running:
            try:
                self._connect_once()
            except Exception as e:
                self.log(f'[连接] 获取直播间信息失败：{e}')
                self.status_msg = '连接失败，重试中'
            if self.running:
                time.sleep(5)

    def _connect_once(self):
        from websocket import WebSocketApp
        import static.Live_pb2 as pb
        from dy_apis.douyin_api import DouyinAPI
        from builder.header import HeaderBuilder
        from builder.params import Params
        from builder.auth import DouyinAuth
        from utils.dy_util import generate_signature

        auth = DouyinAuth()
        auth.perepare_auth(self.cfg['cookie'].strip(), '', '')
        live_id = str(self.cfg['live_id']).strip()

        room_info = DouyinAPI.get_live_info(auth, live_id)
        room_id = room_info['room_id']
        user_id = room_info['user_id']
        res = DouyinAPI.get_webcast_detail(auth, str(user_id), room_id, f'https://live.douyin.com/{live_id}')
        frame = pb.LiveResponse()
        frame.ParseFromString(res)

        params = Params()
        (params
         .add_param('app_name', 'douyin_web').add_param('version_code', '180800')
         .add_param('webcast_sdk_version', '1.0.15').add_param('update_version_code', '1.0.15')
         .add_param('compress', 'gzip').add_param('device_platform', 'web').add_param('cookie_enabled', 'true')
         .add_param('screen_width', '1707').add_param('screen_height', '960')
         .add_param('browser_language', 'zh-CN').add_param('browser_platform', 'Win32')
         .add_param('browser_name', 'Mozilla').add_param('browser_version', HeaderBuilder.ua.split('Mozilla/')[-1])
         .add_param('browser_online', 'true').add_param('tz_name', 'Etc/GMT-8')
         .add_param('cursor', str(frame.cursor)).add_param('internal_ext', frame.internalExt)
         .add_param('host', 'https://live.douyin.com').add_param('aid', '6383').add_param('live_id', '1')
         .add_param('did_rule', '3').add_param('endpoint', 'live_pc').add_param('support_wrds', '1')
         .add_param('user_unique_id', str(user_id)).add_param('im_path', '/webcast/im/fetch/')
         .add_param('identity', 'audience').add_param('need_persist_msg_count', '15')
         .add_param('insert_task_id', '').add_param('live_reason', '').add_param('room_id', room_id)
         .add_param('heartbeatDuration', '0').add_param('signature', generate_signature(room_id, user_id)))
        wss_url = f'wss://webcast100-ws-web-hl.douyin.com/webcast/im/push/v2/?{urlencode(params.get())}'

        self._ws = WebSocketApp(
            url=wss_url,
            header={'Pragma': 'no-cache', 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'User-Agent': HeaderBuilder.ua, 'Upgrade': 'websocket',
                    'Cache-Control': 'no-cache', 'Connection': 'Upgrade'},
            cookie=auth.cookie_str,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=lambda w, e: self.log(f'[连接] 错误：{e}'),
        )
        # 关闭 WSS 证书校验：本机若装了代理/VPN/杀软做 TLS 拦截，证书链会出现自签名证书导致
        # [SSL: CERTIFICATE_VERIFY_FAILED]。此处与 requests 的 verify=False 保持一致。
        import ssl
        self._ws.run_forever(origin='https://live.douyin.com',
                             sslopt={'cert_reqs': ssl.CERT_NONE})

    def _on_open(self, ws):
        self.status_msg = '监听中'
        self.log('[连接] 已连上直播间，开始监听礼物')
        threading.Thread(target=self._ping, args=(ws,), daemon=True).start()

    def _ping(self, ws):
        import static.Live_pb2 as pb
        while self.running:
            f = pb.PushFrame()
            f.payloadType = 'hb'
            try:
                ws.send(f.SerializeToString(), opcode=0x02)
                time.sleep(5)
            except Exception:
                break

    def _on_message(self, ws, message):
        import static.Live_pb2 as pb
        try:
            frame = pb.PushFrame()
            frame.ParseFromString(message)
            response = pb.LiveResponse()
            response.ParseFromString(gzip.decompress(frame.payload))
            if response.needAck:
                ack = pb.PushFrame()
                ack.payloadType = 'ack'
                ack.payload = response.internalExt.encode('utf-8')
                ack.logId = frame.logId
                ws.send(ack.SerializeToString(), opcode=0x02)
            for item in response.messagesList:
                if item.method == 'WebcastGiftMessage':
                    self._handle_gift(item)
                elif item.method == 'WebcastChatMessage' and self.cfg.get('log_all_messages'):
                    m = pb.ChatMessage()
                    m.ParseFromString(item.payload)
                    self.log(f'[弹幕] {m.user.nickname}: {m.content}')
        except Exception:
            pass

    def _handle_gift(self, item):
        import static.Live_pb2 as pb
        g = pb.GiftMessage()
        g.ParseFromString(item.payload)
        name = g.gift.name
        # 记录到"已识别礼物"，供网页快捷选择
        d = self.detected.get(name, {'count': 0})
        d['count'] += 1
        d['last'] = time.strftime('%H:%M:%S')
        self.detected[name] = d
        self.log(f'[礼物] {g.user.nickname} 送出 {name} x{g.comboCount}')
        song = self.cfg.get('gift_songs', {}).get(name)
        if song and self._should_trigger(g.user.id, g.gift.id):
            self.player.enqueue_gift(resolve(song), f'{g.user.nickname} 送「{name}」')


ENGINE = Engine()

# ==================== Web 服务 ====================
app = Flask(__name__, static_folder=None)


def list_media(folder, prefix):
    out = []
    if os.path.isdir(folder):
        for fn in sorted(os.listdir(folder)):
            if os.path.splitext(fn)[1].lower() in ALLOWED_EXT:
                out.append(f'{prefix}/{fn}')
    return out


@app.route('/')
def index():
    return send_from_directory(WEBUI_DIR, 'index.html')


@app.route('/api/config', methods=['GET'])
def get_config():
    cfg = dict(ENGINE.cfg)
    cfg['_songs'] = list_media(SONGS_DIR, 'songs')
    cfg['_bgm_files'] = list_media(BGM_DIR, 'bgm')
    cfg['_common_gifts'] = COMMON_GIFTS
    return jsonify(cfg)


@app.route('/api/config', methods=['POST'])
def set_config():
    data = request.get_json(force=True)
    for k in ('live_id', 'cookie', 'gift_songs', 'bgm_list', 'bgm_enabled', 'bgm_shuffle',
              'combo_window_seconds', 'max_queue', 'volume', 'bgm_volume', 'log_all_messages'):
        if k in data:
            ENGINE.cfg[k] = data[k]
    save_config({k: v for k, v in ENGINE.cfg.items() if not k.startswith('_')})
    ENGINE.log('[配置] 已保存')
    return jsonify(ok=True)


@app.route('/api/upload/<kind>', methods=['POST'])
def upload(kind):
    folder = SONGS_DIR if kind == 'song' else BGM_DIR
    saved = []
    for f in request.files.getlist('files'):
        if not f.filename:
            continue
        if os.path.splitext(f.filename)[1].lower() not in ALLOWED_EXT:
            continue
        name = os.path.basename(f.filename)
        f.save(os.path.join(folder, name))
        saved.append(name)
    ENGINE.log(f'[上传] {kind}: {", ".join(saved) if saved else "无有效文件"}')
    return jsonify(ok=True, saved=saved)


@app.route('/api/delete', methods=['POST'])
def delete_file():
    rel = request.get_json(force=True).get('path', '')
    p = resolve(rel)
    root = os.path.realpath(APP_DIR)
    if os.path.realpath(p).startswith(root) and os.path.isfile(p):
        try:
            os.remove(p)
        except Exception as e:
            return jsonify(ok=False, error=str(e))
    return jsonify(ok=True)


@app.route('/api/start', methods=['POST'])
def api_start():
    ok, msg = ENGINE.start()
    return jsonify(ok=ok, message=msg)


@app.route('/api/stop', methods=['POST'])
def api_stop():
    ok, msg = ENGINE.stop()
    return jsonify(ok=ok, message=msg)


@app.route('/api/test/play', methods=['POST'])
def api_test_play():
    data = request.get_json(force=True)
    rel = data.get('path', '')
    if not rel:
        return jsonify(ok=False, message='未选择文件')
    try:
        vol = max(0.0, min(1.0, float(data.get('volume', ENGINE.cfg.get('volume', 0.9)))))
    except Exception:
        vol = ENGINE.cfg.get('volume', 0.9)
    p = resolve(rel)
    if not os.path.exists(p):
        return jsonify(ok=False, message='文件不存在：' + rel)
    if ENGINE.player.pg is None:
        return jsonify(ok=False, message='音频未初始化，无法试听')
    ENGINE.player.play_test(p, vol)
    return jsonify(ok=True, message=f'试听：{os.path.basename(p)}（音量 {vol:.2f}）')


@app.route('/api/test/stop', methods=['POST'])
def api_test_stop():
    ENGINE.player.stop_current()
    ENGINE.log('[试听] 已停止')
    return jsonify(ok=True, message='已停止试听')


@app.route('/api/bgm/pause', methods=['POST'])
def api_bgm_pause():
    paused = ENGINE.player.toggle_bgm_pause()
    return jsonify(ok=True, paused=paused, message='BGM 已暂停' if paused else 'BGM 已继续')


@app.route('/api/bgm/skip', methods=['POST'])
def api_bgm_skip():
    ENGINE.player.skip_bgm()
    return jsonify(ok=True, message='已切到下一首 BGM')


@app.route('/api/bgm/play', methods=['POST'])
def api_bgm_play():
    rel = request.get_json(force=True).get('path', '')
    if not rel:
        return jsonify(ok=False, message='未选择曲目')
    p = resolve(rel)
    if not os.path.exists(p):
        return jsonify(ok=False, message='文件不存在：' + rel)
    ENGINE.player.play_bgm(p)
    return jsonify(ok=True, message=f'切到：{os.path.basename(p)}')


@app.route('/api/volume', methods=['POST'])
def api_volume():
    """实时设置音量：拖动即生效（tick 每次读取 cfg）；save=true 时才写入 config.json 持久化。"""
    data = request.get_json(force=True)
    kind = data.get('kind')
    key = 'volume' if kind == 'gift' else 'bgm_volume' if kind == 'bgm' else None
    if key is None:
        return jsonify(ok=False, message='kind 需为 gift 或 bgm')
    try:
        val = max(0.0, min(1.0, float(data.get('value'))))
    except Exception:
        return jsonify(ok=False, message='value 不合法')
    ENGINE.cfg[key] = val
    if data.get('save'):
        save_config({k: v for k, v in ENGINE.cfg.items() if not k.startswith('_')})
    return jsonify(ok=True, key=key, value=val)


@app.route('/api/queue/clear', methods=['POST'])
def api_queue_clear():
    n = ENGINE.player.clear_queue()
    return jsonify(ok=True, message=f'已清空 {n} 首')


@app.route('/api/queue/remove', methods=['POST'])
def api_queue_remove():
    idx = request.get_json(force=True).get('index', -1)
    ok = ENGINE.player.remove_queue(int(idx))
    return jsonify(ok=ok, message='已移除' if ok else '移除失败（可能已开始播放）')


@app.route('/obs')
def obs_page():
    return send_from_directory(WEBUI_DIR, 'obs.html')


@app.route('/api/status')
def api_status():
    det = sorted(ENGINE.detected.items(), key=lambda kv: -kv[1]['count'])
    cur = ENGINE.player.current or {}
    return jsonify(
        running=ENGINE.running,
        status=ENGINE.status_msg,
        now_playing=ENGINE.player.now_playing,
        now_playing_kind=cur.get('kind', ''),
        now_playing_reason=cur.get('reason', ''),
        bgm_paused=ENGINE.player.bgm_paused,
        queue=len(ENGINE.player.queue_snapshot()),
        queue_list=ENGINE.player.queue_snapshot(),
        logs=ENGINE.logs()[-120:],
        detected=[{'name': n, 'count': d['count'], 'last': d.get('last', '')} for n, d in det[:30]],
    )


def open_browser(port):
    time.sleep(1.2)
    try:
        webbrowser.open(f'http://127.0.0.1:{port}')
    except Exception:
        pass


def main():
    port = 5001
    print('=' * 54)
    print('  抖音直播间 礼物点歌机 · 网页控制台')
    print(f'  请在浏览器打开： http://127.0.0.1:{port}')
    print('  （关闭此黑窗口即退出程序）')
    print('=' * 54)
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    try:
        app.run(host='127.0.0.1', port=port, threaded=True)
    except OSError:
        port = 5050
        threading.Thread(target=open_browser, args=(port,), daemon=True).start()
        app.run(host='127.0.0.1', port=port, threaded=True)


if __name__ == '__main__':
    main()
