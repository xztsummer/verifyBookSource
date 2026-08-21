import os
import json
import time
import sys
from urllib.parse import urlparse
from book.book import BookSourceManager

def resource_path(relative_path):
    """ 获取资源的绝对路径，适用于开发环境和 PyInstaller 打包后的环境 """
    try:
        # PyInstaller 创建临时文件夹并将路径存储在 _MEIPASS 中
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def load_or_create_config():
    """ 加载或创建配置文件 """
    config_paths = resource_path('book/config.json')

    # 检查是否在 GitHub Actions 环境中运行
    if 'GITHUB_ACTIONS' in os.environ:
        # 在 CI 环境中，尝试加载 config.json
        if os.path.exists(config_paths):
            with open(config_paths, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            # 如果文件不存在，使用环境变量或默认值
            return {
                'path': os.environ.get('BOOK_SOURCE_PATH', ''),
                'outpath': os.environ.get('OUTPUT_PATH', './'),
                'workers': int(os.environ.get('WORKERS', 4)),
                'dedup': os.environ.get('DEDUP', 'true').lower() == 'true',
                'filter': os.environ.get('FILTER', 'false').lower() == 'true',
                'keywords_to_filter': os.environ.get('KEYWORDS_TO_FILTER', '').split(',')
            }
    else:
        # 本地环境
        if input('是否使用config.json文件？（y/n）').lower() == 'n':
            config = {}
            config['path'] = input('本地文件路径/文件直链URL：')
            config['outpath'] = input('书源输出路径（为空则为当前目录，目录最后带斜杠）：') or './'
            config['workers'] = int(input('请输入工作线程，填写数字（并不是越大越好）：'))
            config['dedup'] = input('是否去重？（y/n）').lower() == 'y'
            filter_option = input('是否过滤特定关键词的书源？（y/n）').lower()
            config['filter'] = filter_option == 'y'
            if config['filter']:
                config['keywords_to_filter'] = input('请输入要过滤的关键词，用逗号分隔：').split(',')
            else:
                config['keywords_to_filter'] = []
            with open(config_paths, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4, sort_keys=False)
            return config
        else:
            if not os.path.exists(config_paths):
                print('config.json文件不存在，请检查一下。或者使用命令行输入配置。')
                return None
            with open(config_paths, 'r', encoding='utf-8') as f:
                return json.load(f)

def get_local_source_paths():
    """ 自动发现本地书源文件夹中的书源文件（仅 .json） """
    folder = resource_path(os.path.join('book', '本地书源'))
    if not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
        return []
    return [os.path.join(folder, f) for f in os.listdir(folder)
            if f.lower().endswith('.json')]

def get_network_source_paths():
    """ 从网络书源配置文件读取地址，每行一个URL，空行和 # 开头的注释行忽略 """
    file_path = resource_path(os.path.join('book', '网络书源.txt'))
    if not os.path.exists(file_path):
        with open(file_path, 'w', encoding='utf-8') as f:
            pass
        return []
    with open(file_path, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f
                if line.strip() and not line.strip().startswith('#')]

def get_blocked_domains():
    """ 读取屏蔽网站配置，每行一个域名（支持完整URL，自动取域名并去掉www.），空行和 # 注释行忽略 """
    file_path = resource_path(os.path.join('book', '屏蔽网站.txt'))
    if not os.path.exists(file_path):
        with open(file_path, 'w', encoding='utf-8') as f:
            pass
        return []
    domains = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '://' in line:
                line = urlparse(line).hostname or line
            if line.lower().startswith('www.'):
                line = line[4:]
            if line:
                domains.append(line.lower())
    return list(dict.fromkeys(domains))

def main():
    print("欢迎使用书源校验生成工具（VerifyBookSource v2.1）\n"
          f"{'-' * 16}")

    config = load_or_create_config()
    if config is None:
        return

    local_paths = get_local_source_paths()
    if local_paths:
        print(f"已自动加载本地书源文件夹（book/本地书源）中的 {len(local_paths)} 个文件：")
        for path in local_paths:
            print(f"  - {os.path.basename(path)}")

    network_paths = get_network_source_paths()
    print(f"已从网络书源配置文件（book/网络书源.txt）读取 {len(network_paths)} 个地址")

    blocked_domains = get_blocked_domains()
    if blocked_domains:
        print(f"已从屏蔽网站配置（book/屏蔽网站.txt）读取 {len(blocked_domains)} 个域名: {', '.join(blocked_domains)}")
    config['blocked_domains'] = blocked_domains

    manager = BookSourceManager(local_paths + network_paths, config)
    start_time = time.time()
    results = manager.process_books(int(config['workers']))
    elapsed_time = time.time() - start_time
    manager.save_results(results, config['outpath'])
    analysis = manager.analyze_results(results)




    print(f"\n{'-' * 16}\n"
          "成果报表\n"
          f"书源总数：{analysis['total']}\n"
          f"有效书源数：{analysis['valid']}\n"
          f"无效书源数：{analysis['invalid']}\n"
          f"成功率：{analysis['success_rate']:.2f}%\n"
          f"重复书源数：{analysis['duplicates'] if config.get('dedup') == 'y' else '未选择去重'}\n"
          f"耗时：{elapsed_time:.2f}秒\n")

    # 只在非 GitHub Actions 环境中等待用户输入
    if 'GITHUB_ACTIONS' not in os.environ:
        input('输入任意键退出……')

if __name__ == '__main__':
    main()
