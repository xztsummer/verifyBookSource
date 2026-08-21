import json
import os
import re
import html
import logging
from urllib.parse import urlparse
from urllib3 import disable_warnings
from requests import Session, RequestException
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# 禁用SSL警告，避免在进行HTTPS请求时出现不必要的警告
disable_warnings()

# 常见GBK乱码特征字，出现2个以上即视为乱码（仅收录真实文本中极罕见的字，避免误伤正常中文）
GBK_MOJIBAKE_CHARS = set('锟斤拷鎼佸厛閿燂細锛棰鑾峰彇鏌ヨ涓鍔犺浇鏇柊绔犺妭鐩鑷浠庣繕鏈鏂楗鏄彨')

# 分组关键词 -> 统一形式（符号+文字），按优先级排列，先匹配先命中
GROUP_KEYWORD_MAP = [
    ('轻小说', '🔶 轻小说'),
    ('漫画', '🎨 漫画'),
    ('皇叔', '🔞 皇叔'),
    ('人机验证', '⚠️ 人机验证'),
    ('暂不可用', '⚠️ 暂不可用'),
    ('不可用', '⚠️ 暂不可用'),
    ('精选', '👑 精选'),
    ('优质', '👍 优质'),
    ('普通', '🔰 普通'),
    ('常用', '📌 常用'),
    ('综合', '💠 综合'),
    ('女频', '💐 女频'),
    ('音乐', '🎵 音乐'),
    ('有声', '📻 有声'),
    ('出版', '📚 出版'),
    ('无错', '🌿 无错'),
    ('影视', '🎬 影视'),
    ('视频', '🔞 视频'),
    ('游戏', '🎮 游戏'),
    ('r18', '🔞 R18'),
    ('api', '⭐️ API'),
    ('小说', '📖 小说'),
    ('自定义', '⚙️ 自定义'),
    ('特殊', '🔧 特殊'),
    ('模板', '📐 模板'),
]

# 长相相近的希腊字母转拉丁字母，用于分组匹配（如 ΑΡI 视作 API）
GREEK_LOOKALIKES = str.maketrans({
    'Α': 'A', 'Β': 'B', 'Ε': 'E', 'Ζ': 'Z', 'Η': 'H', 'Ι': 'I',
    'Κ': 'K', 'Μ': 'M', 'Ν': 'N', 'Ο': 'O', 'Ρ': 'P', 'Τ': 'T',
    'Υ': 'Y', 'Χ': 'X',
})

def _looks_garbled(text):
    """ 启发式乱码特征：替换符�、C1控制字符（UTF-8被按Latin-1误解码）、GBK乱码特征字 """
    if '\ufffd' in text:
        return True
    if any('\u0080' <= ch <= '\u009f' for ch in text):
        return True
    return sum(ch in GBK_MOJIBAKE_CHARS for ch in text) >= 2

def _repair_segment(segment):
    """ 还原单个乱码片段：先试Latin-1->UTF-8，再试GBK->UTF-8，无法还原返回原文 """
    try:
        repaired = segment.encode('latin-1').decode('utf-8')
        if not _looks_garbled(repaired):
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    try:
        repaired = segment.encode('gbk').decode('utf-8')
        # GBK乱码还原后长度明显缩短（中文3字节被误解成1.5个字符），且结果应含中文
        if len(repaired) < len(segment) * 0.75 and re.search(r'[\u4e00-\u9fff]', repaired):
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return segment

def repair_mojibake(text):
    """ 还原被错误解码的乱码（UTF-8被按Latin-1或GBK误解码），按段处理支持混排，无法还原的部分保持原样 """
    return re.sub(r'[^\x00-\x7f]+', lambda m: _repair_segment(m.group(0)), text)

def is_garbled_text(text):
    """ 判断文本是否为乱码：含明显乱码特征，或可通过编码还原出不同文本 """
    if not text:
        return False
    if _looks_garbled(text):
        return True
    return repair_mojibake(text) != text

class BookSourceManager:
    def __init__(self, file_paths, config):
        """
        初始化BookSourceManager类
        :param file_paths: str, 书源文件的路径或URL
        :param config: dict, 包含配置参数的字典
        """
        self.file_paths = file_paths
        self.config = config
        self.logger = self.setup_logger()
        self.session = self.setup_session()
        # 预处理过滤关键词，转换为小写并存储为集合，提高查找效率
        self.keywords_set = set(keyword.lower() for keyword in self.config.get('keywords_to_filter', []))

    @staticmethod
    def setup_logger():
        """
        设置日志记录器
        :return: logging.Logger对象
        """
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        return logging.getLogger(__name__)

    def setup_session(self):
        """
        设置requests会话，用于复用连接，提高网络请求效率
        :return: requests.Session对象
        """
        session = Session()
        session.headers.update({'user-agent': self.config.get('user_agent', 'Mozilla/5.0')})
        session.verify = False  # 禁用SSL验证，注意：这可能带来安全风险
        return session

    def load_books(self):
        """
        从文件或URL加载书源
        :return: list, 包含书源数据的列表
        """
        self.logger.info("正在加载书源...")
        books = []
        for file_path in self.file_paths:
            try:
                if file_path.startswith('http'):
                    response = self.session.get(file_path, timeout=self.config.get('timeout', 5))
                    response.raise_for_status()
                    data = response.json()
                else:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                # 文件内容为单个书源对象（dict）时包装为列表，避免 extend 字典键导致后续崩溃
                loaded = data if isinstance(data, list) else [data]
                for book in loaded:
                    if isinstance(book, dict):
                        repaired_count = self.repair_garbled_fields(book)
                        if repaired_count:
                            self.logger.info(f"已还原书源《{book.get('bookSourceName', '?')}》的 {repaired_count} 处乱码字段")
                books.extend(loaded)
            except (RequestException, json.JSONDecodeError, UnicodeDecodeError, FileNotFoundError) as e:
                self.logger.error(f"加载书源时出错: {file_path} - {str(e)}")
        return books

    def repair_garbled_fields(self, obj):
        """
        递归还原书源对象中所有字符串字段的乱码（含嵌套的规则字段）
        :param obj: 书源对象，dict/list/str 均可
        :return: int, 还原的字段数
        """
        count = 0
        if isinstance(obj, dict):
            items = obj.items()
        elif isinstance(obj, list):
            items = enumerate(obj)
        else:
            return 0
        for key, value in items:
            if isinstance(value, str):
                if is_garbled_text(value):
                    repaired = repair_mojibake(value)
                    if repaired != value:
                        obj[key] = repaired
                        count += 1
            else:
                count += self.repair_garbled_fields(value)
        return count

    def check_book_source(self, book):
        """
        检查单个书源的可用性，顺带修复乱码书源名
        :param book: dict, 包含书源信息的字典
        :return: dict, 包含书源和其状态的字典
        """
        try:
            url = book['bookSourceUrl']
            response = self.session.get(url, timeout=self.config.get('timeout', 5))
            status = response.status_code == 200
            self.fix_garbled_name(book, response if status else None)
            return {'book': book, 'status': status}
        except RequestException:
            self.fix_garbled_name(book, None)
            return {'book': book, 'status': False}

    def fix_garbled_name(self, book, response=None):
        """
        修复乱码书源名：优先按Latin-1->UTF-8还原原名，无法还原时改用网页标题
        :param book: dict, 书源信息
        :param response: requests.Response, 书源主页响应（可为None）
        """
        name = book.get('bookSourceName', '')
        if not is_garbled_text(name):
            return
        repaired = repair_mojibake(name)
        if repaired != name:
            book['bookSourceName'] = repaired
            self.logger.info(f"乱码书源名已还原: {repaired} (原: {name})")
            return
        if response is not None:
            title = self.extract_site_title(response)
            if title:
                book['bookSourceName'] = title
                self.logger.info(f"乱码书源名已按网页标题重命名: {title} (原: {name})")

    def extract_site_title(self, response):
        """
        从网页响应中提取站点标题作为书源名
        :param response: requests.Response, 网页响应
        :return: str, 站点名；提取失败返回None
        """
        match = re.search(rb'<title[^>]*>(.*?)</title>', response.content, re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        raw_title = match.group(1).strip()
        charset_match = re.search(rb'charset=["\']?([\w-]+)', response.content[:2048], re.IGNORECASE)
        candidates = []
        if charset_match:
            candidates.append(charset_match.group(1).decode('ascii', errors='ignore'))
        if response.apparent_encoding:
            candidates.append(response.apparent_encoding)
        candidates += ['utf-8', 'gbk']
        for encoding in dict.fromkeys(candidates):
            try:
                title = html.unescape(raw_title.decode(encoding)).strip()
            except (UnicodeDecodeError, LookupError):
                continue
            if not title or is_garbled_text(title):
                continue
            # 标题形如“站点名_其他描述”，取第一段作为书源名
            first_segment = re.split(r'[_|｜·，,：:\-－]+', title)[0].strip()
            return (first_segment or title)[:30]
        return None

    def normalize_group(self, group):
        """
        将分组整理为“符号+文字”的统一形式，多分组以英文逗号分隔
        :param group: str, 原分组
        :return: str, 统一后的分组
        """
        if not group or not group.strip():
            return group
        normalized = []
        for part in re.split(r'[,，、;；]', group):
            part = repair_mojibake(part.strip())
            if not part:
                continue
            # 去掉首尾的符号/emoji（保留中文、拉丁字母数字和希腊字母），再去掉“书源”后缀
            core = re.sub(r'^[^\u4e00-\u9fffA-Za-z0-9\u0370-\u03ff]+', '', part)
            core = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9\u0370-\u03ff]+$', '', core)
            core = re.sub(r'\s*书源$', '', core).strip()
            if not core:
                normalized.append(part)
                continue
            key = core.translate(GREEK_LOOKALIKES).lower()
            canonical = next((name for keyword, name in GROUP_KEYWORD_MAP if keyword in key), None)
            normalized.append(canonical or f'📦 {core}')
        seen, result = set(), []
        for g in normalized:
            if g not in seen:
                seen.add(g)
                result.append(g)
        return ','.join(result)

    @staticmethod
    def is_blocked_url(url, blocked_domains):
        """
        判断书源URL的域名是否命中屏蔽列表（域名本身或其任意子域名）
        :param url: str, 书源URL
        :param blocked_domains: list, 屏蔽域名列表
        :return: bool, 是否被屏蔽
        """
        try:
            hostname = (urlparse(url).hostname or '').lower()
        except ValueError:
            return False
        return any(hostname == domain or hostname.endswith('.' + domain)
                   for domain in blocked_domains)

    def checkbooks(self, workers):
        """
        并发检查所有书源的可用性（屏蔽列表中的书源直接剔除，不发起访问）
        :param workers: int, 并发工作线程数
        :return: dict, 包含有效和无效书源的字典
        """
        self.logger.info("开始检查书源...")
        books = self.load_books()

        blocked_domains = self.config.get('blocked_domains', [])
        if blocked_domains:
            checked_books, blocked_books = [], []
            for book in books:
                if self.is_blocked_url(book.get('bookSourceUrl', ''), blocked_domains):
                    blocked_books.append(book)
                else:
                    checked_books.append(book)
            if blocked_books:
                self.logger.info(f"已屏蔽 {len(blocked_books)} 个书源（命中屏蔽网站列表，未发起访问）")
            books = checked_books

        good, error = [], []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(self.check_book_source, book) for book in books]
            for future in tqdm(as_completed(futures), total=len(books), desc="检查进度"):
                result = future.result()
                (good if result['status'] else error).append(result['book'])

        return {'good': good, 'error': error}

    @staticmethod
    def dedup(books):
        """
        去除重复的书源
        :param books: list, 书源列表
        :return: list, 去重后的书源列表
        """
        seen_urls = set()
        return [book for book in books if not (book['bookSourceUrl'] in seen_urls or seen_urls.add(book['bookSourceUrl']))]

    def should_filter(self, book):
        """
        判断是否应该过滤掉该书源
        :param book: dict, 书源信息
        :return: bool, 是否应该过滤
        """
        if self.config.get('exact_keyword_match', False):
            return book.get('bookSourceName', '').lower() in self.keywords_set
        else:
            return any(
                keyword in book.get('bookSourceName', '').lower() or
                keyword in book.get('bookSourceUrl', '').lower() or
                keyword in book.get('bookSourceGroup', '').lower() or
                keyword in book.get('bookSourceComment', '').lower()
                for keyword in self.keywords_set
            )

    def filter_sources(self, books):
        """
        根据关键词过滤书源
        :param books: list, 待过滤的书源列表
        :return: list, 过滤后的书源列表
        """
        filtered_books = []
        filtered_out = 0
        for book in books:
            if self.should_filter(book):
                filtered_out += 1
                self.logger.debug(f"已过滤书源: {book.get('bookSourceName', 'Unknown')} 由于关键词匹配")
            else:
                filtered_books.append(book)
        self.logger.info(f"共过滤掉 {filtered_out} 个书源")
        return filtered_books

    def process_books(self, workers):
        """
        处理所有书源：检查、去重、过滤
        :param workers: int, 并发工作线程数
        :return: dict, 处理后的结果
        """
        self.logger.info("开始处理书源...")
        result = self.checkbooks(workers)

        duplicates = 0
        if self.config.get('dedup') == 'y':
            self.logger.info("正在去除重复...")
            before_dedup = len(result['good'])
            result['good'] = self.dedup(result['good'])
            duplicates = before_dedup - len(result['good'])
            self.logger.info(f"共去除 {duplicates} 个重复书源")
        result['duplicates'] = duplicates

        if self.config.get('filter', 'n') == 'y':
            self.logger.info("正在过滤书源...")
            result['good'] = self.filter_sources(result['good'])
            result['error'] = self.filter_sources(result['error'])

        self.logger.info("正在整理分组...")
        for book in result['good']:
            book['bookSourceGroup'] = self.normalize_group(book.get('bookSourceGroup', ''))

        self.logger.info("处理完成！")
        return result

    def save_results(self, results, outpath):
        """
        保存处理结果到文件
        :param results: dict, 处理结果
        :param outpath: str, 输出路径
        """
        self.logger.info("正在保存结果...")
        os.makedirs(outpath, exist_ok=True)
        file_path = os.path.join(outpath, 'valid_books.json')
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(results['good'], f, ensure_ascii=False, indent=4, sort_keys=False)
        self.logger.info(f"有效书源已保存到 {file_path}")

    def analyze_results(self, results):
        """
        分析处理结果，提供统计信息
        :param results: dict, 处理结果
        :return: dict, 包含统计信息的字典
        """
        total = len(results['good']) + len(results['error'])
        success_rate = len(results['good']) / total * 100 if total > 0 else 0
        self.logger.info(f"总书源数: {total}")
        self.logger.info(f"有效书源数: {len(results['good'])}")
        self.logger.info(f"无效书源数: {len(results['error'])}")
        self.logger.info(f"成功率: {success_rate:.2f}%")
        return {
            'total': total,
            'valid': len(results['good']),
            'invalid': len(results['error']),
            'success_rate': success_rate,
            'duplicates': results.get('duplicates', 0)
        }
