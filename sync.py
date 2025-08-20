import os
import queue
import shutil
import subprocess
import threading
import time
import json
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from os import scandir, stat
from jinja2 import Environment, FileSystemLoader

CONFIG_FILE = "/usr/python/config.json"
LOG_LINES = []
LOG_LOCK = threading.Lock()
SYNC_THREAD = None
STOP_FLAG = False

# 全局进度/限速
TOTAL_BYTES = 0           # 需要传输的总字节（用于日志/统计，不用于显示总进度条）
DONE_BYTES = 0            # 已传输字节（用于速度统计）
SPEED_LOCK = threading.Lock()
STATUS = {"state": "空闲", "total": 0, "done": 0, "speed_kb": 0}

# 全局令牌桶做总体带宽限制（KB/s）
RATE_LIMIT_KB = 0
TOKEN_BUCKET_BYTES = 0.0
LAST_REFILL = time.time()
TOKEN_LOCK = threading.Lock()

def log(msg):
    with LOG_LOCK:
        LOG_LINES.insert(0, msg)
        if len(LOG_LINES) > 500:
            LOG_LINES.pop()
    print(msg)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "sources": [""],       # 源目录列表
        "destinations": [""],  # 目标目录列表
        "bwlimit": 0,          # KB/s 全局限速
        "auto_time": "",
        "threads": 4,          # 单文件分块线程数
        "parallel_tasks": 2    # 同时传输文件个数
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def refill_tokens():
    """根据当前 RATE_LIMIT_KB 给令牌桶补充令牌，并按新速率收敛桶容量"""
    global TOKEN_BUCKET_BYTES, LAST_REFILL, RATE_LIMIT_KB
    now = time.time()
    rate = RATE_LIMIT_KB  # 当前全局速率(KB/s)，由 consume_tokens 动态更新
    if rate <= 0:
        # 切换为不限速时，清空桶并重置时间，避免旧令牌残留影响
        TOKEN_BUCKET_BYTES = 0.0
        LAST_REFILL = now
        return

    elapsed = now - LAST_REFILL
    if elapsed <= 0:
        return

    capacity = rate * 1024 * 2          # 桶容量 = 2 秒额度，避免突刺
    add = elapsed * rate * 1024         # 补充的字节
    TOKEN_BUCKET_BYTES = min(capacity, TOKEN_BUCKET_BYTES + add)
    LAST_REFILL = now

def consume_tokens(nbytes):
    """全局限速：每次按需消费令牌；bwlimit 在运行中实时读取并立即生效"""
    global RATE_LIMIT_KB, TOKEN_BUCKET_BYTES
    if nbytes <= 0:
        return

    while not STOP_FLAG:
        with TOKEN_LOCK:
            # —— 关键：每次消费前都读取最新配置，让 bwlimit 立即生效 ——
            try:
                RATE_LIMIT_KB = int(load_config().get("bwlimit", 0))
            except Exception:
                RATE_LIMIT_KB = 0

            # 不限速：直接放行
            if RATE_LIMIT_KB <= 0:
                return

            # 先按当前速率补充一下令牌
            refill_tokens()

            # 令牌足够就消费并返回；不够就释放锁小睡一会儿再试
            if TOKEN_BUCKET_BYTES >= nbytes:
                TOKEN_BUCKET_BYTES -= nbytes
                return
        time.sleep(0.01)  # 轻微等待，降低锁竞争

STOP_FLAG = False  # 停止标志

def scan_files(base_path):
    """使用find命令快速获取文件列表"""
    start_time = time.time()
    file_map = {}
    # 使用find命令获取所有文件信息（格式：大小|路径）
    cmd = f"find '{base_path}' -type f ! -name '*.temptd*' -printf '%s|%P\\n' 2>/dev/null"
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, text=True)
    
    with ThreadPoolExecutor() as executor:
        futures = []
        for line in proc.stdout:
            size, path = line.strip().split('|', 1)
            futures.append(executor.submit(file_map.__setitem__, path, int(size)))
        
        # 等待所有任务完成
        for future in futures:
            future.result()
    
    log(f"[扫描完成] {len(file_map)} 个文件，耗时 {time.time()-start_time:.2f}秒")
    return file_map

def build_diff_list(src_root, dst_root):
    """对比差异，只用文件名+大小判断，并累计 TOTAL_BYTES"""
    global TOTAL_BYTES
    diff_files = []
    TOTAL_BYTES = 0
    # 目标目录扫描
    dst_map = scan_files(dst_root)
    #log(f"目标目录文件数: {len(dst_map)}")

    # 源目录扫描
    log("开始扫描源目录并比对...")
    src_map = scan_files(src_root)

    for rel_path, src_size in src_map.items():
        if STOP_FLAG:
            break
        dst_size = dst_map.get(rel_path)
        if dst_size is None:
            diff_files.append((os.path.join(src_root, rel_path),
                               os.path.join(dst_root, rel_path)))
            TOTAL_BYTES += src_size
            log(f"缺失: {rel_path}")
        elif dst_size != src_size:
            diff_files.append((os.path.join(src_root, rel_path),
                               os.path.join(dst_root, rel_path)))
            TOTAL_BYTES += src_size
            log(f"大小不同: {rel_path}")
    return diff_files

def _copy_chunk(src_fd, dst_fd, offset, length):
    """复制单个分块（使用 pread/pwrite 以避免文件指针竞争）"""
    global DONE_BYTES
    remaining = length
    chunk_step = 1024 * 256  # 256KB 小步长，便于响应 STOP 和限速
    while remaining > 0 and not STOP_FLAG:
        step = min(remaining, chunk_step)
        data = os.pread(src_fd, step, offset)
        if not data:
            break
        consume_tokens(len(data))  # 全局限速
        os.pwrite(dst_fd, data, offset)
        with SPEED_LOCK:
            DONE_BYTES += len(data)
        offset += len(data)
        remaining -= len(data)

def copy_file_multithreaded(src_file, dst_file, threads):
    """单文件多线程分块复制（安全 .part + 停止可中断 + 全局限速）"""
    if STOP_FLAG:
        return
    rel_path = "?"  # 仅用于异常日志兜底
    try:
        rel_path = os.path.basename(src_file)
        file_size = os.path.getsize(src_file)
        tmp_dst = dst_file + ".part"

        # 开始日志
        log(f"开始传输: {os.path.relpath(src_file, load_config().get('source',''))}")

        os.makedirs(os.path.dirname(tmp_dst), exist_ok=True)
        # 打开文件描述符
        src_fd = os.open(src_file, os.O_RDONLY)
        dst_fd = os.open(tmp_dst, os.O_CREAT | os.O_RDWR, 0o644)

        try:
            # 预分配/截断到最终大小
            os.ftruncate(dst_fd, file_size)

            # 拆分块
            block_size = 1024 * 1024  # 1MB 大块，再在块内 256KB 步进
            ranges = []
            offset = 0
            while offset < file_size:
                length = min(block_size, file_size - offset)
                ranges.append((offset, length))
                offset += length

            # 分块并发
            with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
                futures = []
                for off, length in ranges:
                    if STOP_FLAG:
                        break
                    futures.append(ex.submit(_copy_chunk, src_fd, dst_fd, off, length))
                for f in as_completed(futures):
                    if STOP_FLAG:
                        break
                    _ = f.result()

        finally:
            os.close(src_fd)
            os.close(dst_fd)

        if STOP_FLAG:
            if os.path.exists(tmp_dst):
                try:
                    os.remove(tmp_dst)
                except Exception:
                    pass
            return

        # 成功完成后原子替换
        os.replace(tmp_dst, dst_file)

    except Exception as e:
        log(f"复制出错({rel_path}): {e}")
        # 清理残留 .part
        try:
            if os.path.exists(dst_file + ".part"):
                os.remove(dst_file + ".part")
        except Exception:
            pass
        raise

def speed_monitor():
    """独立速度统计线程：基于 DONE_BYTES 增量计算全局速度"""
    last = 0
    while STATUS.get("state") == "同步中" and not STOP_FLAG:
        time.sleep(1)
        with SPEED_LOCK:
            cur = DONE_BYTES
        delta = cur - last
        last = cur
        STATUS["speed_kb"] = int(delta / 1024)
    # 结束时清零速度
    STATUS["speed_kb"] = 0

def task_copy_one_file(src_file, dst_file, src_root, per_file_threads):
    """拷贝一个文件（文件级任务）"""
    if STOP_FLAG:
        return
    rel_path = os.path.relpath(src_file, src_root)
    try:
        copy_file_multithreaded(src_file, dst_file, per_file_threads)
        if not STOP_FLAG and os.path.exists(dst_file):
            shutil.copystat(src_file, dst_file)
        if not STOP_FLAG:
            STATUS["done"] += 1
            log(f"[{STATUS['done']}/{STATUS['total']}] 已同步: {rel_path}")
    except Exception as e:
        log(f"文件任务失败: {rel_path} -> {e}")

def sync_worker():
    global STOP_FLAG, STATUS, RATE_LIMIT_KB, TOKEN_BUCKET_BYTES, LAST_REFILL, DONE_BYTES
    cfg = load_config()
    src_roots = cfg.get("sources", [])
    dst_roots = cfg.get("destinations", [])
    per_file_threads = int(cfg.get("threads", 4))          # 单文件分块线程
    parallel_tasks = int(cfg.get("parallel_tasks", 2))     # 同时传输的文件数
    RATE_LIMIT_KB = int(cfg.get("bwlimit", 0))

    # 限速桶初始化
    TOKEN_BUCKET_BYTES = 0.0
    LAST_REFILL = time.time()
    DONE_BYTES = 0

    if not src_roots or not dst_roots:
        log("❌ 源目录或目标目录未配置")
        STATUS["state"] = "空闲"
        return

    STATUS["state"] = "同步中"
    STATUS["total"] = 0
    STATUS["done"] = 0

    for idx, (src_root, dst_root, is_enabled) in enumerate(zip(src_roots, dst_roots, cfg.get("enabled", [True] * len(src_roots)))):
        if STOP_FLAG:
            log(f"⏹ 已停止，跳过剩余目录")
            break

        if not is_enabled:
            log(f"⚪ 第 {idx+1} 对未启用: {src_root} -> {dst_root}，跳过")
            continue

        if not src_root or not dst_root:
            log(f"⚠️ 第 {idx+1} 对源/目标路径为空，跳过")
            continue

        log(f"🔍 扫描第 {idx+1} 对源/目标: {src_root} -> {dst_root}")

        # 生成差异文件列表
        diff_list = build_diff_list(src_root, dst_root)
        STATUS["total"] = len(diff_list)
        STATUS["done"] = 0
        log(f"发现 {len(diff_list)} 个差异文件需要同步")

        if STOP_FLAG or not diff_list:
            continue

        # 启动速度监控线程
        monitor = threading.Thread(target=speed_monitor, daemon=True)
        monitor.start()
        log(f"启动线程")
        # 并行文件同步
        with ThreadPoolExecutor(max_workers=max(1, parallel_tasks)) as file_pool:
            futures = []
            for src_file, dst_file in diff_list:
                if STOP_FLAG:
                    break
                os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                futures.append(file_pool.submit(
                    task_copy_one_file, src_file, dst_file, src_root, per_file_threads
                ))

            # 等待已提交任务完成
            for f in as_completed(futures):
                if STOP_FLAG:
                    pass

        if STOP_FLAG:
            log(f"⏹ 第 {idx+1} 对源/目标同步中途停止")
            break
        else:
            log(f"✅ 第 {idx+1} 对源/目标同步完成")

    STATUS["state"] = "完成" if not STOP_FLAG else "已停止"
    if not STOP_FLAG:
        log("🎉 全部同步完成")
    else:
        log("⏹ 同步被停止，部分文件未完成")

    STOP_FLAG = False  # 重置

def auto_start_checker():
    last_run_times = set()  # 防止同一分钟重复启动
    while True:
        time.sleep(30)  # 每 30 秒检查一次
        cfg = load_config()
        auto_times = cfg.get("auto_time", "")
        if auto_times:
            now = time.strftime("%H:%M")
            times_list = [t.strip() for t in auto_times.split("|") if t.strip()]
            if now in times_list and now not in last_run_times:
                global SYNC_THREAD, STOP_FLAG
                if not (SYNC_THREAD and SYNC_THREAD.is_alive()):
                    STOP_FLAG = False
                    SYNC_THREAD = threading.Thread(target=sync_worker)
                    SYNC_THREAD.start()
                    log(f"⏰ 自动启动: {now}")
                last_run_times.add(now)
            # 清理过期时间，避免无限增长
            last_run_times = {t for t in last_run_times if t in times_list}

# Flask 网页
app = Flask(__name__)

TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Python 同步工具</title>
<style>
    /* 基础样式 */
    body {
      background: #f8f9fa;
      color: #333;
      font-family: Arial, sans-serif;
      margin: 0;
      padding: 20px;
    }

    h1 {
      text-align: center;
      color: #007bff;
    }

    form {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 20px;
    }

    .form-group {
      display: flex;
      flex-direction: column;
      width: 100%;
    }

    .form-group label {
      font-weight: bold;
      margin-bottom: 5px;
    }

    .form-group input,
    .form-group button {
      padding: 8px;
      font-size: 14px;
      width: 100%;
    }

    .form-group input[type="text"] {
      font-size: 16px;
      width: calc(100% - 100px); /* Input takes all available space except for the button */
    }

    button {
      background-color: #007bff;
      color: white;
      border: none;
      cursor: pointer;
      padding: 10px 20px;  /* 设置统一的内边距 */
      font-size: 16px;      /* 统一字体大小 */
      width: 200px;         /* 设置按钮的宽度，使其一致 */
      margin: 10px 0;       /* 为按钮添加上下间距 */
      border-radius: 5px;   /* 给按钮添加圆角 */
      transition: background-color 0.3s;
    }

    button:hover {
      background-color: #0056b3;
    }

    .form-group button {
      width: 100px;
    }

    .form-group input[type="text"] {
      width: calc(100% - 120px); /* Ensure button stays aligned */
    }

    .progress {
      width: 100%;
      background: #ccc;
      height: 20px;
      margin: 20px 0;
      border-radius: 5px;
    }

    .bar {
      background-color: green;
      height: 100%;
      width: 0;
      border-radius: 5px;
    }

    textarea {
      width: 100%;
      height: 300px;
      background: black;
      color: #0f0;
      font-family: monospace;
      padding: 10px;
      border-radius: 5px;
      resize: none;
      border: none;
    }

    /* 响应式设计：针对小屏设备进行调整 */
    @media (max-width: 768px) {
      .form-group input,
      .form-group button {
        width: 100%;
      }

      .form-group {
        width: 100%;
      }

      .form-group input[type="text"] {
        width: calc(100% - 110px);
      }
    }

    /* 表格容器 */
  #dir-list-grid {
    border-collapse: collapse; /* 合并边框 */
    margin: 20px 0 0 0;
    width: 90%;
  }

  /* 表头样式 */
  #dir-list-grid thead {
    width: 100%;
    background-color: #f2f2f2; /* 浅灰色背景 */
  }

  #dir-list-grid th {
    padding: 12px 15px;
    text-align: center;
    font-weight: bold;
    color: #333;
  }

  /* 表格单元格样式 */
  #dir-list-grid td {
    padding: 12px 15px;
    text-align: center;
    vertical-align: middle;
    border: 1px solid #ddd; /* 边框颜色 */
  }

  /* 输入框样式 */
  #dir-list-grid input[type="text"] {
    width: 95%; /* 输入框宽度 */
    padding: 8px;
    font-size: 14px;
    text-align: left;
    border: 1px solid #ccc;
    border-radius: 4px;
  }

  /* 按钮容器（使按钮按一行排列） */
  .button-container {
    display: flex;
    justify-content: space-between;
    gap: 10px;  /* 按钮之间的间距 */
  }

  /* 按钮样式 */
  #dir-list-grid button {
    padding: 6px 12px;
    font-size: 14px;
    color: #fff;
    background-color: #007bff;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    transition: background-color 0.3s;
    width: 90px;  /* 设置固定宽度 */
  }

  #dir-list-grid button:hover {
    background-color: #0056b3; /* 鼠标悬停时的背景色 */
  }

  /* 删除按钮的特殊样式 */
  #dir-list-grid button[type="button"]:last-child {
    background-color: #dc3545;
  }

  #dir-list-grid button[type="button"]:last-child:hover {
    background-color: #c82333;
  }

  /* 表格行的隔行换色效果 */
  #dir-list-grid tbody tr:nth-child(even) {
    background-color: #f9f9f9;
  }

  

  /* 使表格内容更居中 */
  #dir-list-grid td, #dir-list-grid th {
    text-align: center;
  }

  /* 添加行按钮样式：在表格下面单独换行 */
  .add-row-container {
    display: block;      /* 独占一行 */
    width: 100%;
    margin-top: 12px;
    clear: both;         /* 若上面有 float，确保换行 */
    text-align: left;    /* 改成 center/right 可控制对齐 */
  }

  .add-row-container .add-row-button {
    display: inline-block;
    width: 160px;        /* 自定义宽度 */
    color: #fff;
    background-color: #218838; /* 默认绿色 */
    border: none;
    border-radius: 4px;
    cursor: pointer;
    transition: background-color 0.3s;
    text-align: center;
    margin-top: 12px;        /* 与表格分开 */
    padding: 6px 12px;
    font-size: 14px;
    /* 其它样式随意 */
  }

  .add-row-container .add-row-button:hover {
    background-color: #1e7e34;
  }
</style>
</head>
<body>

<h1>Python 同步工具</h1>

<form method="post" action="/save">

  <table id="dir-list-grid">
    <thead>
      <tr>
        <th width="5%">序号</th>
        <th width="5%">启用</th>
        <th width="35%">源地址</th>
        <th width="35%">目标地址</th>
        <th width="20%">操作</th>
      </tr>
    </thead>
    <tbody>
      <!-- 每行表示一个源和目标地址 -->
      {% for i, source in enumerate(config.sources) %}
        <tr>
          <td>{{ i+1 }}</td>
          <td>
            <input type="checkbox" name="enabled_{{i}}" value="1"
                 {% if config.enabled and config.enabled[i] %}checked{% endif %}>
          </td>
          <td><input type="text" name="source_{{i}}" value="{{ source }}" readonly></td> 
          <td><input type="text" name="dest_{{i}}" value="{{ config.destinations[i] }}" readonly></td>
          <td class="button-container">
            <button type="button" onclick="openDirSelector({{i}}, 'source')">选择源</button>
            <button type="button" onclick="openDirSelector({{i}}, 'dest')">选择目标</button>
            <button type="button" onclick="removeRow(this)">删除</button>
          </td>
        </tr>
      {% endfor %}
    </tbody>
  </table>

  <div class="add-row-container">
    <button type="button" class="add-row-button" onclick="addRow()">添加源/目标</button>
  </div>

  <div class="form-group">
    <label for="bwlimit">带宽限制(KB/s):</label>
    <input type="text" name="bwlimit" value="{{config.bwlimit}}">
  </div>

  <div class="form-group">
    <label for="parallel_tasks">并行任务数:</label>
    <input type="text" name="parallel_tasks" value="{{config.parallel_tasks}}">
  </div>

  <div class="form-group">
    <label for="threads">线程数(单文件):</label>
    <input type="text" name="threads" value="{{config.threads}}">
  </div>

  <div class="form-group">
    <label for="auto_time">自动执行时间(HH:MM)，支持多个时间，|符号间隔:</label>
    <input type="text" name="auto_time" value="{{config.auto_time}}">
  </div>

  <div class="form-group">
    <button type="submit">保存配置</button>
    <button type="button" onclick="restartService()">重启服务</button>
  </div>
</form>

<div style="text-align: center;">
  <button onclick="fetch('/start').then(r => r.json()).then(alert)">启动同步</button>
  <button onclick="fetch('/stop').then(r => r.json()).then(alert)">停止同步</button>
</div>

<h2>状态</h2>
<div id="status"></div>
<div class="progress">
  <div class="bar" id="bar"></div>
</div>

<h2>日志</h2>
<textarea id="log" readonly></textarea>

<style>
  /* 仅用于目录选择器的简单样式 */
  #dir-dialog { 
    display: none; 
    position: absolute; 
    top: 100px; 
    left: 50%; 
    transform: translateX(-50%);  /* 居中对齐 */
    background: #fff; 
    border: 1px solid #ccc; 
    padding: 10px; 
    z-index: 999;
    width: 90%; /* 默认宽度为90%屏幕宽度 */
    max-width: 420px; /* 最大宽度为420px */
    box-sizing: border-box;
    border-radius: 8px; /* 增加圆角 */
  }
  #current-path { 
    margin: 0; 
    padding: 5px; 
    border-bottom: 1px solid #ddd; 
    font-size: 14px; 
    word-break: break-word; /* 防止路径过长 */
  }
  #dir-list { 
    height: 320px; 
    overflow: auto; 
    border: 1px solid #ddd; 
    padding: 5px;
    border-radius: 4px;
    margin-bottom: 10px;
  }
  .dir-row { 
    cursor: pointer; 
    padding: 10px 14px; 
    border-radius: 4px; 
    transition: background 0.3s ease;
  }
  .dir-row:hover, .dir-row:active { 
    background: #eef; 
  }
  .dir-row.sel { 
    background: #cce5ff; 
  }

  /* 优化小屏幕的按钮布局 */
  #dir-dialog button {
    display: block;
    width: 100%;
    padding: 10px;
    margin-top: 10px;
    border: 1px solid #ccc;
    background: #007bff;
    cursor: pointer;
  }

  #dir-dialog button:hover {
    background-color: #eef;
  }

  /* 响应式设计，适配不同屏幕 */
  @media (max-width: 600px) {
    #dir-dialog {
      width: 95%; /* 在手机上占据更大的屏幕宽度 */
      padding: 12px;
    }

    .dir-row {
      padding: 12px 16px; /* 增加触摸区域 */
    }

    #current-path {
      font-size: 12px; /* 减小字体 */
    }
  }
</style>

<div id="dir-dialog">
  <h3 id="current-path"></h3>
  <div id="dir-list"></div>
  <div style="margin-top: 10px; text-align: right;">
    <button type="button" onclick="createDir()">新建目录</button>
    <button type="button" onclick="selectCurrentDir()">选择当前目录</button>
    <button type="button" onclick="selectSubDir()">选择下级目录</button>
    <button type="button" onclick="closeDialog()">取消</button>
  </div>
</div>


<script>
let currentField = "";
let currentPath = "/";  // 根路径
let selectedSubDir = "";
let currentRow = -1; // 当前编辑的行

function addRow() {
  const table = document.getElementById("dir-list-grid").getElementsByTagName('tbody')[0];
  const row = table.insertRow();
  const index = table.rows.length;

  // 序号
  let cell = row.insertCell(0);
  cell.innerText = index;

  // 源地址
  cell = row.insertCell(1);
  cell.innerHTML = `<input type="text" name="source_${index-1}" value="" readonly>`;
  
  // 目标地址
  cell = row.insertCell(2);
  cell.innerHTML = `<input type="text" name="dest_${index-1}" value="" readonly>`;
  
  // 操作按钮
  cell = row.insertCell(3);
  cell.innerHTML = `
    <button type="button" onclick="openDirSelector(${index-1}, 'source')">选择源</button>
    <button type="button" onclick="openDirSelector(${index-1}, 'dest')">选择目标</button>
    <button type="button" onclick="removeRow(this)">删除</button>
  `;
}

function removeRow(button) {
  const row = button.closest('tr');
  row.remove();
}

// 模拟双击延时（以避免单次点击进入目录）
let lastClickTime = 0;
const DOUBLE_CLICK_DELAY = 300;  // 延迟时间（毫秒）

// 加载目录列表
function loadDir(path) {
  fetch(`/list_dir?path=${encodeURIComponent(path)}`)
    .then(r => r.json())
    .then(data => {
      if (data.error) { alert(data.error); return; }
      currentPath = data.path || "/";
      selectedSubDir = "";
      document.getElementById("current-path").innerText = currentPath;
      const list = document.getElementById("dir-list");
      list.innerHTML = "";

      // 添加返回上一层的目录项
      if (currentPath !== "/") {
        const up = document.createElement("div");
        up.textContent = "..";  // 上一级目录
        up.className = "dir-row";

        // 返回上一层目录的功能：单击或触摸触发
        up.onclick = () => loadDir(parentPath(currentPath));
        up.ontouchstart = () => loadDir(parentPath(currentPath));  // 支持触摸设备

        list.appendChild(up);
      }

      // 添加当前目录的文件夹列表
      (data.dirs || []).forEach(d => {
        const row = document.createElement("div");
        row.textContent = d;
        row.className = "dir-row";

        // 选择目录：点击或触摸时高亮并回填路径
        row.onclick = () => {
          document.querySelectorAll("#dir-list .dir-row").forEach(el => el.classList.remove("sel"));
          row.classList.add("sel");
          selectedSubDir = d;
          const chosen = joinPath(currentPath, d);
          document.getElementById(currentField).value = chosen;  // 回填路径到对应输入框
        };

        // 双击进入目录
        row.ondblclick = () => loadDir(joinPath(currentPath, d));

        // 触摸设备支持：双击进入目录
        row.ontouchstart = row.onclick;
        row.ontouchend = row.ondblclick;

        list.appendChild(row);
      });
    });
}

// 连接路径
function joinPath(base, name) {
  if (!base || base === "/") return "/" + name;
  return base.replace(/\/+$/,"") + "/" + name;
}

// 返回上一级目录
function parentPath(p) {
  if (!p || p === "/") return "/";
  const parts = p.split("/").filter(Boolean);
  parts.pop();
  return "/" + parts.join("/");
}

// 打开目录选择对话框
function openDirSelector(rowIndex, field) {
  currentRow = rowIndex;
  currentField = field;
  const input = document.querySelector(`input[name=${field}_${rowIndex}]`);
  currentPath = input.value || "/";  // 获取当前路径或默认 "/"
  loadDir(currentPath);  // 加载目录
  document.getElementById("dir-dialog").style.display = "block";  // 显示弹框
}

// 关闭目录选择框
function closeDialog() {
  document.getElementById("dir-dialog").style.display = "none";  // 隐藏弹框
}

// 选择目录后回填
function selectCurrentDir() {
  const input = document.querySelector(`input[name=${currentField}_${currentRow}]`);
  input.value = currentPath;
  closeDialog();
}

function selectSubDir() {
  if (!selectedSubDir) {
    alert("请先单击选择一个下级目录");
    return;
  }

  // 获取正确的输入框（与 selectCurrentDir 一致）
  const input = document.querySelector(`input[name=${currentField}_${currentRow}]`);
  
  // 回填选中的下级目录
  const newPath = joinPath(currentPath, selectedSubDir);
  input.value = newPath;

  // 更新 currentPath 为新选择的路径（这样下一次可以选择其下级目录）
  currentPath = newPath;

  // 清空 selectedSubDir，防止重复使用
  selectedSubDir = "";

  // 关闭目录选择对话框
  closeDialog();
}

function createDir() {
  const name = prompt("请输入新目录名称：");
  if (!name) return;
  fetch(`/mkdir?path=${encodeURIComponent(currentPath)}&name=${encodeURIComponent(name)}`)
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        alert("创建失败: " + data.error);
      } else {
        loadDir(currentPath); // 刷新目录列表
      }
    });
}

function restartService() {
    if (!confirm("确定要重启 sync 服务吗？")) return;
    fetch("/restart", {
        method: "POST"
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            alert("sync 服务已重启成功");
        } else {
            alert("重启失败: " + (data.error || "未知错误"));
        }
    })
    .catch(e => {
        //alert("请求失败: " + e);
    });
}

function update(){
  fetch('/status').then(r=>r.json()).then(s=>{
    document.getElementById('status').innerText = 
      "状态: " + s.state + 
      " | 完成: " + s.done + "/" + s.total +
      " | 速度: " + s.speed_kb + " KB/s";
    let pct = (s.total > 0) ? (s.done / s.total * 100).toFixed(1) : 0;
    document.getElementById('bar').style.width = pct + "%";
  });
  fetch('/log').then(r=>r.json()).then(d=>{
    document.getElementById('log').value = d.join("\\n");
  });
}
setInterval(update, 1000);
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(TEMPLATE, config=load_config(), enumerate=enumerate)

@app.route("/save", methods=["POST"])
def save():
    # 获取源地址和目标地址的列表
    sources = []
    destinations = []
    enabled = []
    i = 0
    
    # 获取所有的 source_{{i}} 和 dest_{{i}} 字段
    while f"source_{i}" in request.form and f"dest_{i}" in request.form:
        sources.append(request.form[f"source_{i}"])
        destinations.append(request.form[f"dest_{i}"])
        # 如果复选框在表单里出现，说明启用；否则就是禁用
        enabled.append(f"enabled_{i}" in request.form)
        i += 1

    # 其他配置项
    bwlimit = int(request.form.get('bwlimit', 0))
    auto_time = request.form.get('auto_time', '')
    parallel_tasks = int(request.form.get('parallel_tasks', 2))
    threads = int(request.form.get('threads', 4))

    # 保存配置到文件
    config = {
        "sources": sources,
        "destinations": destinations,
        "enabled": enabled,   # ✅ 新增
        "bwlimit": bwlimit,
        "auto_time": auto_time,
        "parallel_tasks": parallel_tasks,
        "threads": threads
    }

    save_config(config)
    # 保存完成后重定向回首页，浏览器会重新加载页面
    return redirect(url_for('index'))

def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

@app.route("/start")
def start():
    global SYNC_THREAD, STOP_FLAG
    if SYNC_THREAD and SYNC_THREAD.is_alive():
        return jsonify("同步已在进行中")
    STOP_FLAG = False
    SYNC_THREAD = threading.Thread(target=sync_worker)
    SYNC_THREAD.start()
    return jsonify("同步已启动")

@app.route("/stop")
def stop():
    global STOP_FLAG
    STOP_FLAG = True
    return jsonify("停止请求已发送")

@app.route("/status")
def get_status():
    return jsonify(STATUS)

@app.route("/log")
def get_log():
    with LOG_LOCK:
        return jsonify(LOG_LINES)

@app.route("/list_dir")
def list_dir():
    path = request.args.get("path", "/")
    if not os.path.isdir(path):
        return jsonify({"error": "不是有效目录"}), 400
    try:
        # 只返回下级目录，按名称排序
        dirs = sorted(
            [name for name in os.listdir(path) 
             if os.path.isdir(os.path.join(path, name))]
        )
    except PermissionError:
        return jsonify({"error": "权限不足"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"path": path, "dirs": dirs})

@app.route("/mkdir")
def mkdir():
    path = request.args.get("path", "/")
    name = request.args.get("name", "")
    if not name:
        return jsonify({"error": "目录名不能为空"}), 400
    new_path = os.path.join(path, name)
    try:
        os.makedirs(new_path, exist_ok=False)
    except FileExistsError:
        return jsonify({"error": "目录已存在"}), 400
    except PermissionError:
        return jsonify({"error": "权限不足"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"success": True, "path": new_path})

@app.route("/restart", methods=["POST"])
def restart_service():
    import subprocess
    try:
        # 如果运行 Flask 的不是 root，这里建议用 sudo 并在 visudo 里配置免密码
        subprocess.run(["/etc/init.d/sync", "restart"], check=True)
        return jsonify({"success": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=auto_start_checker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
