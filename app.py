from flask import Flask, render_template, request, jsonify
import sqlite3
import schedule
import time
import threading
import requests
from bs4 import BeautifulSoup
import re
import random
import logging
from datetime import datetime, timedelta
import json
import os
import openai
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
DB_PATH = 'articles.db'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Referer': 'https://www.woshipm.com/'
}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """初始化数据库表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 启用 WAL 模式，提高并发性能
    cursor.execute('PRAGMA journal_mode=WAL')
    
    # 创建笔记表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_link TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (article_link) REFERENCES articles(link) ON DELETE CASCADE
        )
    ''')
    
    conn.commit()
    conn.close()

# 初始化数据库
init_db()

def parse_date(date_str):
    """解析日期字符串，返回 datetime 对象"""
    try:
        # 处理类似 "2025-12-25 14:30" 的格式
        return datetime.strptime(date_str, '%Y-%m-%d %H:%M')
    except:
        try:
            # 处理其他可能的格式
            return datetime.strptime(date_str, '%Y-%m-%d')
        except:
            return None

def get_article_stats(detail_url):
    try:
        response1 = requests.get(detail_url, headers=HEADERS, timeout=10)
        if response1.status_code == 200:
            soup1 = BeautifulSoup(response1.text, 'html.parser')
            container = soup1.find('div', class_='meta--sup__right')
            if container:
                text = container.get_text()
                comments = re.search(r'(\d+)\s*评论', text)
                views = re.search(r'(\d+)\s*浏览', text)
                collections = re.search(r'(\d+)\s*收藏', text)
                comment_num = comments.group(1) if comments else '0'
                view_num = views.group(1) if views else '0'
                collect_num = collections.group(1) if collections else '0'
                container1 = soup1.find('div', class_='article--content')
                length_num = len(container1.text.strip()) if container1 else '0'
                container2 = soup1.find('time')
                article_time = container2.text if container2 else ''
                # 获取文章内容
                content = container1.get_text(strip=True) if container1 else ''

                # 清理用户介绍（包含"本文由"、"发布于"、"作者"等关键词）
                if content:
                    # 查找用户介绍的起始位置
                    intro_patterns = ['本文由', '发布于', '作者：', '作者:', '原创发布于', '未经授权']
                    for pattern in intro_patterns:
                        if pattern in content:
                            idx = content.find(pattern)
                            if idx > 0:
                                content = content[:idx].strip()
                                break

                return comment_num, view_num, collect_num, length_num, article_time, content
    except Exception as e:
        logger.error(f'解析详情页出错: {detail_url}, 错误: {e}')
    return '0', '0', '0', '0', '', ''

def process_article_data(comments, views, collects, length, desc, content):
    """处理文章数据：类型转换和内容清理"""
    comments = int(comments) if comments else 0
    views = int(views) if views else 0
    collects = int(collects) if collects else 0
    length = int(length) if length else 0

    # 从内容中移除 desc（导言）
    if content and desc:
        if content.startswith(desc):
            content = content[len(desc):].strip()
        elif desc in content:
            content = content.replace(desc, '', 1).strip()

    return comments, views, collects, length, content

def save_article_to_db(cursor, link, title, article_time, desc, length, views, collects, comments, content, exists=False):
    """保存文章到数据库"""
    if exists:
        cursor.execute(
            'UPDATE articles SET views = ?, comments = ?, collections = ?, content = ? WHERE link = ?',
            (views, comments, collects, content, link)
        )
    else:
        cursor.execute(
            '''INSERT INTO articles (link, title, time, desc, length, views, collections, comments, read, content)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (link, title, article_time, desc, length, views, collects, comments, 0, content)
        )

def scrape_woshipm_ai(url, cursor, mode):
    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        logger.error('无法访问列表页')
        return
    soup = BeautifulSoup(response.text, 'html.parser')
    articles = soup.select('.postlist-item')
    logger.info(f'开始抓取，预计获取 {len(articles)} 篇文章...')
    for post in articles:
        title_element = post.select_one('.post-title a')
        if not title_element:
            continue
        title = title_element.get('title')
        link = title_element.get('href')
        desc_element = post.select_one('.des')
        desc = desc_element.find(string=True, recursive=False).strip()

        cursor.execute('SELECT * FROM articles WHERE link = ?', (link,))
        rows = cursor.fetchall()
        if len(rows) == 0 or (len(rows) > 0 and mode == 0):
            logger.info(f'获取文章信息并更新数据库: {title}')
            comments, views, collects, length, article_time, content = get_article_stats(link)
            comments, views, collects, length, content = process_article_data(comments, views, collects, length, desc, content)

            save_article_to_db(cursor, link, title, article_time, desc, length, views, collects, comments, content, exists=len(rows) > 0)
            time.sleep(random.uniform(3, 5))

def catchup_woshipm_ai(url, cursor, mode, min_date=None):
    """补齐历史数据，支持按日期停止抓取"""
    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        logger.error(f'无法访问列表页: {url}')
        return False, None  # 返回是否继续抓取的标志和最后日期

    soup = BeautifulSoup(response.text, 'html.parser')
    articles = soup.select('.postlist-item')
    logger.info(f'页面 {url} 获取到 {len(articles)} 篇文章')

    should_continue = True
    last_date = None

    for post in articles:
        title_element = post.select_one('.post-title a')
        if not title_element:
            continue
        title = title_element.get('title')
        link = title_element.get('href')
        desc_element = post.select_one('.des')
        desc = desc_element.find(string=True, recursive=False).strip()

        cursor.execute('SELECT * FROM articles WHERE link = ?', (link,))
        rows = cursor.fetchall()

        # 检查是否已存在，如果存在则跳过
        if len(rows) > 0:
            logger.info(f'文章已存在，跳过: {title}')
            # 从数据库中获取文章时间用于判断是否继续抓取
            existing_time = rows[0][3]
            article_date = parse_date(existing_time)
            if article_date:
                last_date = article_date
                # 如果设置了最小日期，检查是否需要继续
                if min_date and article_date < min_date:
                    logger.info(f'文章日期 {article_date} 早于最小日期 {min_date}，停止抓取')
                    should_continue = False
                    break
            continue

        # 获取文章详情
        comments, views, collects, length, article_time, content = get_article_stats(link)
        comments, views, collects, length, content = process_article_data(comments, views, collects, length, desc, content)

        # 解析文章日期
        article_date = parse_date(article_time)
        if article_date:
            last_date = article_date
            logger.info(f'文章日期: {article_date}')

            # 如果设置了最小日期，检查是否需要继续
            if min_date and article_date < min_date:
                logger.info(f'文章日期 {article_date} 早于最小日期 {min_date}，停止抓取')
                should_continue = False
                break

        # 新增文章
        logger.info(f'新增文章: {title}')
        save_article_to_db(cursor, link, title, article_time, desc, length, views, collects, comments, content, exists=False)

        # 礼貌抓取，避免被封 IP
        time.sleep(random.uniform(3, 5))

    return should_continue, last_date

def update_articles(auto_score=False):
    logger.info('=' * 50)
    logger.info('开始定时任务: 更新文章数据')
    logger.info(f'执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    logger.info(f'自动评分: {auto_score}')
    logger.info('=' * 50)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        mode = 1  # 仅新增模式
        for i in range(1, 4):
            url = 'https://www.woshipm.com/ai/page/' + str(i)
            scrape_woshipm_ai(url, cursor, mode)
            conn.commit()
            time.sleep(random.uniform(3, 5))
        conn.close()
        logger.info('定时任务完成: 文章数据更新成功')

        # 如果启用自动评分，对最近7天的文章进行评分
        if auto_score:
            logger.info('开始自动评价最近7天的文章...')
            auto_score_new_articles(days=7)
            logger.info('自动评价完成')
    except Exception as e:
        logger.error(f'定时任务执行失败: {e}')

def auto_score_new_articles(days=7):
    """自动评价最近N天的文章"""
    try:
        # 计算N天前的日期
        cutoff_date = datetime.now() - timedelta(days=days)
        cutoff_date_str = cutoff_date.strftime('%Y-%m-%d')
        
        conn = get_db_connection()
        cursor = conn.cursor()
        # 获取未评分且在最近N天内的文章
        cursor.execute('''
            SELECT link, title, time 
            FROM articles 
            WHERE (score IS NULL OR score = 0) 
            AND time >= ?
            ORDER BY time DESC
        ''', (cutoff_date_str,))
        articles = cursor.fetchall()
        conn.close()
        
        if not articles:
            logger.info(f'最近 {days} 天内没有需要评分的文章')
            return
        
        logger.info(f'找到 {len(articles)} 篇最近 {days} 天内需要评分的文章')
        
        for article in articles:
            link = article['link']
            title = article['title']
            article_time = article['time']
            logger.info(f'开始自动评分: {title} ({article_time})')
            
            try:
                # 调用评分逻辑
                evaluation_result = evaluate_article_internal(link, title)
                if evaluation_result:
                    logger.info(f'自动评分成功: {title}, 得分: {evaluation_result.get("overall_score", 0)}')
                else:
                    logger.error(f'自动评分失败: {title}')
            except Exception as e:
                logger.error(f'自动评分出错: {title}, 错误: {e}')
            
            # 评分间隔，避免过快请求
            time.sleep(2)
            
    except Exception as e:
        logger.error(f'自动评分失败: {e}')

def evaluate_article_internal(link, title):
    """内部评分函数，用于自动评分"""
    try:
        # 获取文章内容
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT content, desc FROM articles WHERE link = ?', (link,))
        row = cursor.fetchone()
        conn.close()
        
        article_content = None
        if row and row['content']:
            article_content = row['content']
        else:
            # 从网页获取
            article_content = fetch_article_content(link)
            if article_content:
                # 清理 desc
                if row and row['desc'] and article_content.startswith(row['desc']):
                    article_content = article_content[len(row['desc']):].strip()
                
                # 保存到数据库
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute('UPDATE articles SET content = ? WHERE link = ?', (article_content, link))
                conn.commit()
                conn.close()
        
        if not article_content:
            return None
        
        # 调用大模型评分
        evaluation_result = evaluate_article_with_llm(title, article_content)

        if evaluation_result:
            # 保存评分结果
            overall_score = 0
            if 'overall_score' in evaluation_result:
                overall_score = evaluation_result['overall_score']
            elif '综合评价' in evaluation_result and 'overall_score' in evaluation_result['综合评价']:
                overall_score = evaluation_result['综合评价']['overall_score']
            else:
                # 如果没有 overall_score，计算各维度分数的平均值
                dimensions = ['信息增量', '逻辑严密性', '可理解性/易读性', '结构组织', '行动启发']
                scores = []
                for dim in dimensions:
                    if dim in evaluation_result and isinstance(evaluation_result[dim], dict):
                        score = evaluation_result[dim].get('score', 0)
                        scores.append(score)
                if scores:
                    overall_score = round(sum(scores) / len(scores), 1)
                    logger.info(f'自动计算综合评分: {overall_score} (基于各维度分数: {scores})')

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE articles
                SET score = ?, score_details = ?
                WHERE link = ?
            ''', (
                overall_score,
                json.dumps(evaluation_result, ensure_ascii=False),
                link
            ))
            conn.commit()
            conn.close()

            return evaluation_result
        
        return None
        
    except Exception as e:
        logger.error(f'内部评分失败: {e}')
        return None

def run_scheduler():
    logger.info('定时任务调度器启动')
    # 每天下午 3 点执行
    schedule.every().day.at("15:00").do(update_articles)
    while True:
        schedule.run_pending()
        time.sleep(60)

# Web 路由
@app.route('/')
def index():
    conn = get_db_connection()
    cursor = conn.cursor()

    show_marked = request.args.get('show_marked', 'all')
    page = int(request.args.get('page', 1))
    per_page = 10  # 每页显示10篇文章

    # 构建查询条件
    if show_marked == 'marked':
        cursor.execute('SELECT COUNT(*) FROM articles WHERE read = 1')
        total = cursor.fetchone()[0]
        cursor.execute('SELECT * FROM articles WHERE read = 1 ORDER BY time DESC LIMIT ? OFFSET ?',
                      (per_page, (page - 1) * per_page))
    else:
        cursor.execute('SELECT COUNT(*) FROM articles')
        total = cursor.fetchone()[0]
        cursor.execute('SELECT * FROM articles ORDER BY time DESC LIMIT ? OFFSET ?',
                      (per_page, (page - 1) * per_page))

    articles = cursor.fetchall()
    conn.close()

    # 计算分页信息
    total_pages = (total + per_page - 1) // per_page
    
    # 生成页码范围（显示当前页前后的页码）
    page_range = []
    if total_pages <= 7:
        page_range = list(range(1, total_pages + 1))
    else:
        if page <= 4:
            page_range = [1, 2, 3, 4, 5, '...', total_pages]
        elif page >= total_pages - 3:
            page_range = [1, '...', total_pages - 4, total_pages - 3, total_pages - 2, total_pages - 1, total_pages]
        else:
            page_range = [1, '...', page - 1, page, page + 1, '...', total_pages]
    
    logger.info(f'分页信息: 总页数={total_pages}, 当前页={page}, 页码范围={page_range}')

    return render_template('index.html',
                         articles=articles,
                         show_marked=show_marked,
                         current_page=page,
                         total_pages=total_pages,
                         page_range=page_range)

@app.route('/toggle_read', methods=['POST'])
def toggle_read():
    link = request.json.get('link')
    read_status = request.json.get('read', 0)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE articles SET read = ? WHERE link = ?', (read_status, link))
    conn.commit()
    conn.close()

    return jsonify({'success': True})

@app.route('/stats')
def stats():
    conn = get_db_connection()
    cursor = conn.cursor()

    # 使用单个查询获取所有统计信息
    cursor.execute('''
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN read = 1 THEN 1 ELSE 0 END) as read_count,
            SUM(CASE WHEN read = 0 THEN 1 ELSE 0 END) as unread_count
        FROM articles
    ''')
    result = cursor.fetchone()

    conn.close()

    return jsonify({
        'total': result['total'],
        'read': result['read_count'],
        'unread': result['unread_count']
    })

@app.route('/update_now', methods=['POST'])
def update_now():
    """手动触发立即更新"""
    data = request.get_json() or {}
    auto_score = data.get('auto_score', False)
    
    def run_update():
        update_articles(auto_score=auto_score)

    thread = threading.Thread(target=run_update)
    thread.start()
    return jsonify({'success': True, 'message': '正在更新文章数据...'})

@app.route('/catchup_data', methods=['POST'])
def catchup_data():
    """手动触发补齐历史数据"""
    data = request.get_json() or {}
    start_date_str = data.get('start_date')  # 格式: '2025-12-01'
    max_pages = data.get('max_pages', 100)
    mode = data.get('mode', 0)  # 0: 强制更新所有数据, 1: 仅抓取新增文章
    auto_score = data.get('auto_score', False)

    logger.info('=' * 50)
    logger.info('开始补齐历史数据')
    logger.info(f'执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    logger.info(f'起始日期: {start_date_str}')
    logger.info(f'最大页数: {max_pages}')
    logger.info(f'模式: {mode}')
    logger.info(f'自动评分: {auto_score}')
    logger.info('=' * 50)

    def run_catchup():
        try:
            # 解析起始日期
            min_date = None
            if start_date_str:
                min_date = parse_date(start_date_str)
                if not min_date:
                    logger.error(f'日期格式错误: {start_date_str}，请使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM 格式')
                    return

            conn = get_db_connection()
            cursor = conn.cursor()

            # 从第1页开始抓取
            page = 1

            while page <= max_pages:
                url = f'https://www.woshipm.com/ai/page/{page}'
                logger.info(f'正在抓取第 {page} 页...')

                should_continue, last_date = catchup_woshipm_ai(url, cursor, mode, min_date)
                conn.commit()

                if not should_continue:
                    logger.info(f'第 {page} 页已达到最小日期限制，停止抓取')
                    break

                page += 1
                # 页面之间也添加延迟
                time.sleep(random.uniform(3, 5))

            conn.close()

            logger.info('=' * 50)
            logger.info('历史数据补齐完成')
            logger.info(f'共抓取 {page - 1} 页')
            logger.info('=' * 50)

            # 如果启用自动评分，对最近7天的文章进行评分
            if auto_score:
                logger.info('开始自动评价最近7天的文章...')
                auto_score_new_articles(days=7)
                logger.info('自动评价完成')
        except Exception as e:
            logger.error(f'补齐历史数据失败: {e}')

    thread = threading.Thread(target=run_catchup)
    thread.start()
    return jsonify({'success': True, 'message': '正在补齐历史数据...'})

@app.route('/status')
def status():
    """获取服务状态"""
    next_run = schedule.next_run()
    return jsonify({
        'status': 'running',
        'next_update': next_run.strftime('%Y-%m-%d %H:%M:%S') if next_run else None
    })

# AI 评分提示词
EVAL_PROMPT = """# Role

你是一位拥有10万+订阅的知识博主主编，擅长筛选深度、有趣且严谨的科普/专业文章。你能够客观、犀利地识别内容的含金量。

# Task

请根据我提供的【评价量表】，对下述文章进行分维度打分，并给出最终的综合评价。

# Rubrics (评分量表)

请根据以下 5 个维度进行评分，每个维度 1-5 分：

1. 信息增量 (Information Density)

5分：提供了新颖的见解、前沿的研究或深刻的底层逻辑。

1分：内容陈词滥调，全是互联网已知信息的简单拼凑。

2. 逻辑严密性 (Logical Rigor)

5分：推导过程环环相扣，论据（数据、案例）能强力支撑论点。

1分：论证存在逻辑谬误，结论跳跃，论据不可靠。

3. 可理解性/易读性 (Accessibility)

5分：化繁为简，善用类比，排版清晰，即使外行也能读懂。

1分：充斥晦涩难懂的术语且不加解释，语言啰嗦。

4. 结构组织 (Structure)

5分：标题抓人，导语引人入胜，小标题具有高度概括性，结尾有总结升华。

1分：文章像流水账，缺乏视觉层级，读起来很累。

5. 行动启发 (Actionability)

5分：读者看完后能获得明确的方法论或认知维度的提升，产生"恍然大悟"感。

1分：纯情绪输出或纯理论堆砌，无实际参考价值。

# Constraints (要求)

证据优先：在给出分数前，必须引用文章中的具体原文或段落作为打分依据。

拒绝中庸：除非文章表现中庸，否则尽量拉开评分档次。

# Evaluation Format (输出格式)

json格式的维度评分结果，包含各个维度和整体的分数、一句话评价，以及文章概述（300-500字）。

JSON格式示例：
{
  "信息增量": {"score": 5, "comment": "..."},
  "逻辑严密性": {"score": 4, "comment": "..."},
  "可理解性/易读性": {"score": 5, "comment": "..."},
  "结构组织": {"score": 5, "comment": "..."},
  "行动启发": {"score": 4, "comment": "..."},
  "综合评价": {
    "overall_score": 4.6,
    "summary": "..."
  },
  "文章概述": "文章主要讲述了...（300-500字）"
}

---

待评价文章内容如下：
"""

def fetch_article_content(url):
    """获取文章完整内容"""
    logger.info(f'开始获取文章内容，URL: {url}')

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        logger.info(f'HTTP 响应状态码: {response.status_code}')

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            content_div = soup.find('div', class_='article--content')

            if content_div:
                content = content_div.get_text(strip=True)

                # 清理用户介绍（包含"本文由"、"发布于"、"作者"等关键词）
                if content:
                    intro_patterns = ['本文由', '发布于', '作者：', '作者:', '原创发布于', '未经授权']
                    for pattern in intro_patterns:
                        if pattern in content:
                            idx = content.find(pattern)
                            if idx > 0:
                                content = content[:idx].strip()
                                logger.info(f'清理用户介绍，移除 "{pattern}" 之后的内容')
                                break

                logger.info(f'文章内容获取成功，长度: {len(content)} 字符')
                return content
            else:
                logger.warning('未找到文章内容 div (class="article--content")')
        else:
            logger.warning(f'HTTP 请求失败，状态码: {response.status_code}')

        return None
    except requests.Timeout:
        logger.error(f'获取文章内容超时: {url}')
        return None
    except Exception as e:
        logger.error(f'获取文章内容失败: {url}, 错误: {e}', exc_info=True)
        return None

def evaluate_article_with_llm(article_title, article_content):
    """使用大模型对文章进行评分"""
    logger.info(f'开始调用大模型评分，文章标题: {article_title}')

    logger.info(f'评价提示词加载成功，长度: {len(EVAL_PROMPT)} 字符')

    # 构建完整的提示词
    full_prompt = f"{EVAL_PROMPT}\n\n文章标题：{article_title}\n\n文章内容：\n{article_content}"

    logger.info(f'完整提示词长度: {len(full_prompt)} 字符，文章内容长度: {len(article_content)} 字符')

    try:
        # 从 .env 文件读取配置
        api_key = os.getenv('LLM_API_KEY')
        base_url = os.getenv('LLM_API_BASE', 'https://api.openai.com/v1')
        model = os.getenv('LLM_MODEL', 'gpt-4o-mini')
        temperature = float(os.getenv('LLM_TEMPERATURE', '0.3'))

        logger.info(f'大模型配置 - API Base: {base_url}, Model: {model}, Temperature: {temperature}')

        if not api_key:
            logger.error('未设置 LLM_API_KEY 环境变量')
            return None

        logger.info(f'API Key 已设置，长度: {len(api_key)} 字符')

        client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url
        )

        logger.info('开始调用大模型 API...')

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一位专业的文章评价专家，擅长对科普和专业文章进行多维度评分。"},
                {"role": "user", "content": full_prompt}
            ],
            temperature=temperature,
            response_format={"type": "json_object"}
        )

        logger.info(f'API 调用成功，响应状态: {response.choices[0].finish_reason}')

        result = response.choices[0].message.content
        logger.info(f'原始响应内容长度: {len(result)} 字符')
        logger.info(f'原始响应内容: {result[:500]}...')  # 只打印前500字符

        # 清理 markdown 代码块标记
        result = result.strip()
        if result.startswith('```json'):
            result = result[7:]  # 移除 ```json
        if result.startswith('```'):
            result = result[3:]  # 移除 ```
        if result.endswith('```'):
            result = result[:-3]  # 移除结尾的 ```
        result = result.strip()

        logger.info(f'清理后的响应内容长度: {len(result)} 字符')

        parsed_result = json.loads(result)
        logger.info(f'JSON 解析成功，结果类型: {type(parsed_result)}')

        return parsed_result

    except json.JSONDecodeError as e:
        logger.error(f'JSON 解析失败: {e}')
        logger.error(f'原始响应内容: {result}')
        return None
    except Exception as e:
        logger.error(f'调用大模型评分失败: {e}', exc_info=True)
        return None

@app.route('/evaluate_article', methods=['POST'])
def evaluate_article():
    """对文章进行评分"""
    link = request.json.get('link')
    title = request.json.get('title')

    logger.info(f'收到评分请求 - Link: {link}, Title: {title}')

    if not link or not title:
        logger.error('缺少必要参数')
        return jsonify({'success': False, 'message': '缺少必要参数'})

    logger.info(f'开始评价文章: {title}')

    # 优先从数据库获取文章内容
    logger.info('从数据库获取文章内容...')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT content, desc FROM articles WHERE link = ?', (link,))
    row = cursor.fetchone()
    conn.close()

    article_content = None
    if row and row['content']:
        article_content = row['content']
        logger.info(f'从数据库获取文章内容成功，长度: {len(article_content)} 字符')
    else:
        logger.info('数据库中没有文章内容，尝试从网页获取...')
        article_content = fetch_article_content(link)
        if article_content:
            # 从数据库获取 desc 并清理
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT desc FROM articles WHERE link = ?', (link,))
            desc_row = cursor.fetchone()
            desc = desc_row['desc'] if desc_row else None
            conn.close()
            
            # 清理 desc
            if desc and article_content.startswith(desc):
                article_content = article_content[len(desc):].strip()
                logger.info(f'清理 desc，移除开头的导言')
            
            # 保存到数据库
            logger.info('将文章内容保存到数据库...')
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('UPDATE articles SET content = ? WHERE link = ?', (article_content, link))
            conn.commit()
            conn.close()
            logger.info('文章内容保存成功')

    if not article_content:
        logger.error('无法获取文章内容')
        return jsonify({'success': False, 'message': '无法获取文章内容'})

    logger.info(f'文章内容获取成功，长度: {len(article_content)} 字符')

    # 调用大模型进行评分
    logger.info('开始调用大模型进行评分...')
    evaluation_result = evaluate_article_with_llm(title, article_content)

    if evaluation_result:
        logger.info(f'大模型评分成功，结果: {evaluation_result}')

        # 提取总分，支持多种可能的格式
        overall_score = 0
        if 'overall_score' in evaluation_result:
            overall_score = evaluation_result['overall_score']
            logger.info(f'从顶层字段提取总分: {overall_score}')
        elif '综合评价' in evaluation_result and 'overall_score' in evaluation_result['综合评价']:
            overall_score = evaluation_result['综合评价']['overall_score']
            logger.info(f'从嵌套字段提取总分: {overall_score}')
        else:
            # 如果没有 overall_score，计算各维度分数的平均值
            dimensions = ['信息增量', '逻辑严密性', '可理解性/易读性', '结构组织', '行动启发']
            scores = []
            for dim in dimensions:
                if dim in evaluation_result and isinstance(evaluation_result[dim], dict):
                    score = evaluation_result[dim].get('score', 0)
                    scores.append(score)
            if scores:
                overall_score = round(sum(scores) / len(scores), 1)
                logger.info(f'自动计算综合评分: {overall_score} (基于各维度分数: {scores})')
            else:
                logger.warning('未找到各维度分数，使用默认值 0')

        # 将评分结果保存到数据库
        try:
            logger.info('开始保存评分结果到数据库...')
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE articles
                SET score = ?, score_details = ?
                WHERE link = ?
            ''', (
                overall_score,
                json.dumps(evaluation_result, ensure_ascii=False),
                link
            ))
            conn.commit()
            conn.close()

            logger.info(f'文章评分完成: {title}, 得分: {overall_score}')
            return jsonify({'success': True, 'result': evaluation_result})
        except Exception as e:
            logger.error(f'保存评分结果失败: {e}', exc_info=True)
            return jsonify({'success': False, 'message': '保存评分结果失败'})
    else:
        logger.error('大模型评分失败，返回结果为 None')
        return jsonify({'success': False, 'message': '评分失败，请检查大模型配置'})

@app.route('/article_score')
def article_score():
    """获取文章评分详情"""
    link = request.args.get('link')
    if not link:
        return jsonify({'success': False, 'message': '缺少必要参数'})

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT score, score_details FROM articles WHERE link = ?', (link,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return jsonify({
                'success': True,
                'score': row['score'],
                'score_details': row['score_details']
            })
        else:
            return jsonify({'success': False, 'message': '文章不存在'})
    except Exception as e:
        logger.error(f'获取评分详情失败: {e}')
        return jsonify({'success': False, 'message': '获取评分详情失败'})

@app.route('/save_note', methods=['POST'])
def save_note():
    """保存或更新笔记"""
    link = request.json.get('link')
    content = request.json.get('content', '')
    
    if not link:
        return jsonify({'success': False, 'message': '缺少必要参数'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 检查是否已存在笔记
        cursor.execute('SELECT id FROM notes WHERE article_link = ?', (link,))
        existing = cursor.fetchone()
        
        if existing:
            # 更新现有笔记
            cursor.execute('''
                UPDATE notes 
                SET content = ?, updated_at = CURRENT_TIMESTAMP 
                WHERE article_link = ?
            ''', (content, link))
        else:
            # 创建新笔记
            cursor.execute('''
                INSERT INTO notes (article_link, content) 
                VALUES (?, ?)
            ''', (link, content))
        
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': '笔记保存成功'})
    except Exception as e:
        logger.error(f'保存笔记失败: {e}')
        return jsonify({'success': False, 'message': '保存笔记失败'})

@app.route('/get_note', methods=['GET'])
def get_note():
    """获取文章的笔记"""
    link = request.args.get('link')
    
    if not link:
        return jsonify({'success': False, 'message': '缺少必要参数'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, content, created_at, updated_at 
            FROM notes 
            WHERE article_link = ?
        ''', (link,))
        note = cursor.fetchone()
        conn.close()
        
        if note:
            return jsonify({
                'success': True,
                'note': {
                    'id': note['id'],
                    'content': note['content'],
                    'created_at': note['created_at'],
                    'updated_at': note['updated_at']
                }
            })
        else:
            return jsonify({'success': True, 'note': None})
    except Exception as e:
        logger.error(f'获取笔记失败: {e}')
        return jsonify({'success': False, 'message': '获取笔记失败'})

@app.route('/delete_note', methods=['POST'])
def delete_note():
    """删除笔记"""
    link = request.json.get('link')
    
    if not link:
        return jsonify({'success': False, 'message': '缺少必要参数'})
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM notes WHERE article_link = ?', (link,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'message': '笔记删除成功'})
    except Exception as e:
        logger.error(f'删除笔记失败: {e}')
        return jsonify({'success': False, 'message': '删除笔记失败'})

if __name__ == '__main__':
    logger.info('人人都是产品经理 AI 专栏爬虫服务启动')
    logger.info('Web 服务地址: http://localhost:5000')
    logger.info('定时任务将在每天 15:00 自动更新数据')

    # 启动定时任务线程
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # 启动 Flask 应用
    app.run(host='0.0.0.0', port=5000, debug=True)