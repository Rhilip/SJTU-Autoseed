# 环境依赖
# pip3 install bencode.py cn2an requests python-qbittorrent bs4 lxml

import os
import re
import json
import hashlib
import logging
import random
import subprocess
import argparse

import bencode
import cn2an
import requests
import qbittorrent

from glob import glob
from logging.handlers import RotatingFileHandler

from bs4 import BeautifulSoup

# 属性设置

# qBittorrent
# qbt 中需要设置完成命令，使得脚本能捕捉qbt完成动作  /path/to/python3 /path/to/autoseed/run.py -i "%I" -n "%N"
# qbt 中需要启用 “复制 .torrent 文件到” 或 “复制下载完成的 .torrent 文件到”
qbt_address = 'http://127.0.0.1:2017/'
qbt_user = ''
qbt_password = ''

# Putao 帐号信息（Cookies，Passkey）
putao_passkey = ''
putao_cookies_raw = ''

# 豆瓣API KEY，用于搜索简介
douban_apikey = [
    "0dad551ec0f84ed02907ff5c42e8ec70",
    "02646d3fb69a52ff072d47bf23cef8fd"
]

# 标题正则
PTN = re.compile(
    # Series (Which name match with 0day Source, see https://scenerules.org/t.html?id=tvx2642k16.nfo 16.4)
    r"\.?(?P<full_name>(?P<search_name>[\w\-. ]+?)[. ]"
    r"(?P<episode_full>([Ss](?P<season>\d+))?[Ee][Pp]?(?P<episode>\d+)(-[Ee]?[Pp]?\d+)?|[Ss]\d+|Complete).+?"
    r"(HDTV|WEB-DL|WEB|HDTVrip).+?(-(?P<group>.+?))?)"
    r"(\.(?P<filetype>\w+)$|$)"
)

# Pt-Gen API位置 (建议使用cf-worker版)
ptgen_api = 'https://api.rhilip.info/tool/movieinfo/gen'
# ptgen_api = 'https://api.nas.ink/infogen'
# ptgen_api = 'https://ptgen.rhilip.info/'

# 得到的值为 /path/to/autoseed ，该目录下需要创建 cache 文件夹
base_path = os.path.dirname(__file__)

fake_ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.106 Safari/537.36'

# -- 日志相关 --
instance_log_file = os.path.join(base_path, 'autoseed.log')

logging_datefmt = "%m/%d/%Y %I:%M:%S %p"
logging_format = "%(asctime)s - %(levelname)s - %(funcName)s - %(message)s"

logFormatter = logging.Formatter(fmt=logging_format, datefmt=logging_datefmt)

logger = logging.getLogger()
logger.setLevel(logging.NOTSET)
while logger.handlers:  # Remove un-format logging in Stream, or all of messages are appearing more than once.
    logger.handlers.pop()

if instance_log_file:
    fileHandler = RotatingFileHandler(filename=instance_log_file, mode='a', maxBytes=5 * 1024 * 1024, backupCount=2)
    fileHandler.setFormatter(logFormatter)
    logger.addHandler(fileHandler)

consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(logFormatter)
logger.addHandler(consoleHandler)


def cookies_raw2jar(raw: str) -> dict:
    """
    Arrange Cookies from raw using SimpleCookies
    """
    if not raw:
        raise ValueError("The Cookies is not allowed to be empty.")

    from http.cookies import SimpleCookie
    cookie = SimpleCookie(raw)
    return {key: morsel.value for key, morsel in cookie.items()}


putao_cookies = cookies_raw2jar(putao_cookies_raw)


def get_douban_apikey():
    return random.choice(douban_apikey)


class AutoseedStopException(Exception):
    pass


class Autoseed:
    # 待转发种子信息，从qbt动作中获取
    info_hash: str = None
    torrent_name: str = ''

    # qBittorrent 对象
    qbt: qbittorrent.Client = None
    qbt_preference: dict = None

    # 待转发种子信息，从qbt api中获取
    torrent_properties = None
    torrent_trackers: list = []
    torrent_file_path: str = None

    # 待转发种子简介相关信息
    torrent_name_ptn: dict = None
    torrent_descr: dict = None

    def __init__(self):
        self.parse_argv()

    def run(self):
        self.get_torrent_info_from_qbt()  # 从qbt中获取种子的详细信息
        self.is_new_torrent()  # 检查种子是否为新种子，不过不是新种子则会抛出 AutoseedStopException
        self.post_to_putao()  # 发布到 PUTAO，并将发布后的种子添加到qbt

    def parse_argv(self):
        """
        解析 qbt 调用命令，并从中获取到info_hash, torrent_name 等信息，并将信息写入 self.argv
        """
        parse = argparse.ArgumentParser()
        parse.add_argument('-i', help="Info hash of completed torrent")
        parse.add_argument('-n', help="Name of completed torrent")
        argv = parse.parse_args()
        self.info_hash = argv.i
        self.torrent_name = argv.n
        logger.info('qBittorrent 命令解析完成，得到新完成种子 "%s" ，其info_hash值为 "%s"', self.torrent_name, self.info_hash)

    def get_qbt_instance(self) -> qbittorrent.Client:
        if not isinstance(self.qbt, qbittorrent.Client):
            logger.info('开始连接qBittorrent.......')
            qbt = qbittorrent.Client(qbt_address)
            qbt.login(qbt_user, qbt_password)
            logger.info('qBittorrent 连接成功， 版本 %s (WebAPI %s)', qbt.qbittorrent_version, qbt.api_version)

            self.qbt = qbt
            self.qbt_preference = qbt.preferences()

        return self.qbt

    def get_torrent_info_from_qbt(self):
        qbt = self.get_qbt_instance()
        logger.info('开始获取种子信息')
        self.torrent_properties = qbt.get_torrent(self.info_hash)
        self.torrent_trackers = qbt.get_torrent_trackers(self.info_hash)

    def is_new_torrent(self):
        """
        遍历待发布tracker，通过url判断是不是需要autoseed
        """
        for tracker in self.torrent_trackers:
            tracker = tracker.get('url')
            # 这三个属性值跳过
            if tracker == "** [DHT] **" or tracker == "** [PeX] **" or tracker == "** [LSD] **":
                pass

            if tracker.find('tracker.sjtu.edu.cn') > -1:
                raise AutoseedStopException('该种子为 PUTAO 已发布种子，自动跳过，不再重新尝试发布')

    def post_to_putao(self):
        """
        发布主方法
        """
        # 发布元信息准备
        torrent_file = self.get_torrent_file()  # 获得待发布的种子文件路径
        torrent_descr = self.get_torrent_descr()  # 获得待发布种子简介 （PT-GEN格式）

        # 发布表单准备
        title = '[%s%s] %s' % (  # 标题  [地球百子 第六季 第07集] The 100 S06E07 720p HDTV x264-SVA
            torrent_descr.get('chinese_title'),  # 地球百子 第六季
            ' 第{}集'.format(self.torrent_name_ptn['episode']) if self.torrent_name_ptn.get('episode') else '',  # 第07集
            self.torrent_name_ptn.get('full_name').replace('.', ' ')  # The 100 S06E07 720p HDTV x264-SVA
        )
        sub_title = ''  # 副标题 留空
        imdb_link = torrent_descr.get('imdb_link', '')  # IMDb链接
        douban_link = 'https://movie.douban.com/subject/%s' % (torrent_descr['sid'],) if torrent_descr.get(
            'sid') else ''  # 豆瓣链接

        # 简介
        descr = re.sub(r"\u3000", "　", torrent_descr.get('format'))
        mediainfo = self.get_torrent_mediainfo()
        if mediainfo:
            descr += '\n[font=Courier New][quote=MediaInfo (自动生成，仅供参考)]{info}[/quote][/font]'.format(info=mediainfo)
        type_ = 410  # 类型 直接指定 欧美电视剧
        isoday = 'yes'  # 直接指定是 0day

        codec_sel = 1  # 编码 直接指定 H.264

        # 分辨率
        standard_sel = 3  # 分辨率默认为 720p
        if self.torrent_name.find('1080p') > -1:
            standard_sel = 1  # 分辨率改成 1080p

        post_file = {
            'file': (
                os.path.basename(torrent_file).encode("ascii", errors="ignore").decode(),
                open(torrent_file, 'rb'),
                'application/x-bittorrent'
            )
        }
        post_form = [
            ('name', title),
            ('small_descr', sub_title),
            ('url', imdb_link),
            ('douban_url', douban_link),
            ('descr', descr),
            ('type', type_),
            ('isoday', isoday),
            ('codec_sel', codec_sel),
            ('standard_sel', standard_sel)
        ]

        # 开始发布
        logger.info('发布资源准备完成，开始发布')

        upload_url = 'https://pt.sjtu.edu.cn/takeupload.php'
        try:
            post = requests.post(upload_url, files=post_file, data=post_form, cookies=putao_cookies)
        except Exception as e:
            raise AutoseedStopException('发布失败，服务器可能无响应 %s' % e)

        # 发布完成，检查发布状态
        if post.url != upload_url:  # 说明成功发布，并从中获取到种子id
            logger.info('发布成功，新种子链接为 %s', post.url)
            seed_torrent_download_id = re.search(r"id=(\d+)", post.url).group(1)
            self.send_new_torrent_to_qbt(seed_torrent_download_id)
        else:  # 发布失败，搜索原因
            outer_message = self.torrent_upload_err_message(post.text)
            raise AutoseedStopException('发布失败，原因为 %s' % outer_message)

    def send_new_torrent_to_qbt(self, tid):
        torrent_link = 'https://pt.sjtu.edu.cn/download.php?id={}&passkey={}'.format(tid, putao_passkey)  # 构造种子链接
        self.qbt.download_from_link(torrent_link)  # 添加到qbt中

    @staticmethod
    def torrent_upload_err_message(post_text) -> str:
        outer_bs = BeautifulSoup(post_text, "lxml")
        outer_tag = outer_bs.find("td", id="outer")
        if outer_tag.find_all("table"):  # Remove unnecessary table info(include SMS,Report)
            for table in outer_tag.find_all("table"):
                table.extract()
        outer_message = outer_tag.get_text().replace("\n", "")
        return outer_message

    def get_torrent_name_ptn(self) -> dict:
        search = re.search(PTN, self.torrent_name)
        if search:
            torrent_name_ptn = search.groupdict()
            if torrent_name_ptn.get('season'):
                torrent_name_ptn['season_cn'] = cn2an.an2cn(torrent_name_ptn.get('season'))

            self.torrent_name_ptn = torrent_name_ptn
            return self.torrent_name_ptn

        # 说明种子命名不符合我们的要求
        raise AutoseedStopException('待发布种子 %s 不符合发布文件命名规则，跳过' % (self.torrent_name,))

    def get_torrent_descr(self) -> dict:
        """
        0day美剧一般命名格式为
         - The.Bold.Type.S03E03.Stroke.Of.Genius.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb.mkv
         - The.100.S06E07.720p.HDTV.x264-SVA.mkv
        将其转化为 The.100 第六季 并通过 豆瓣搜索接口 + Pt-gen 获取第一个资源简介
        """
        tname_ptn = self.get_torrent_name_ptn()  # 解析发布种子命名

        # The.100 S06.cache.json
        descr_cache_key = '%s.S%s.cache.json' % (tname_ptn.get('search_name'), tname_ptn.get('season'))
        cache_file = os.path.join(base_path, 'cache', descr_cache_key)
        if os.path.exists(cache_file):  # 从本地缓存简介中读取
            with open(cache_file, 'r', encoding='utf-8') as f:
                desc = json.load(f)
        else:
            desc = self.search_info_from_douban_and_ptgen()
            # 缓存获取到的简介
            if not os.path.exists(cache_file):  # 这里重新检查一次，防止可能有的另一个进程同样在生成简介，因为 x 操作符禁止覆盖已有文件
                with open(cache_file, 'x', encoding='utf-8') as f:
                    json.dump(desc, f, ensure_ascii=False, sort_keys=True, indent=2)

        self.torrent_descr = desc
        return self.torrent_descr

    def search_info_from_douban_and_ptgen(self) -> dict:
        # 整理成 The.100 第六季  直接使用 The.100 第06季可能会出现问题
        douban_search_title = self.torrent_name_ptn.get('search_name')
        if self.torrent_name_ptn.get('season_cn', '一') != '一':
            douban_search_title += ' 第%s季' % self.torrent_name_ptn.get('season_cn', '一')

        # 通过豆瓣API获取到豆瓣链接
        logger.info('使用关键词 %s 在豆瓣搜索', douban_search_title)
        try:
            r = requests.get('https://api.douban.com/v2/movie/search',
                             params={'q': douban_search_title, 'apikey': get_douban_apikey()},
                             headers={'User-Agent': fake_ua})
            rj = r.json()
            ret: dict = rj['subjects'][0]  # 基本上第一个就是我们需要的233333
        except Exception as e:
            raise AutoseedStopException('豆瓣未返回正常结果，报错如下 %s' % (e,))

        logger.info('获得到豆瓣信息, 片名: %s , 豆瓣链接: %s', ret.get('title'), ret.get('alt'))

        # 通过Pt-GEN接口获取详细简介
        douban_url = ret.get('alt')
        logger.info('通过Pt-GEN 获取资源 %s 详细简介', douban_url)

        r = requests.get(ptgen_api, params={'url': douban_url}, headers={'User-Agent': fake_ua})
        rj = r.json()
        if rj.get('success', False):
            return rj
        else:  # Pt-GEN接口返回错误
            raise AutoseedStopException('Pt-GEN 返回错误，错误原因 %s' % (rj.get('error', '')))

    @staticmethod
    def _mediainfo(file) -> str:
        logger.info('获取文件 %s 的Mediainfo信息 ', file)
        process = subprocess.Popen(["mediainfo", file], stdout=subprocess.PIPE)
        output, error = process.communicate()

        if not error and output != b"\n":
            output = output.decode()  # bytes -> string
            output = re.sub(re.escape(file), os.path.basename(file), output)  # Hide file path
            return output
        else:
            return ''

    def get_torrent_mediainfo(self) -> str:
        path = os.path.join(self.qbt_preference.get('save_path'), self.torrent_name)
        if os.path.isfile(path):  # 单文件
            return self._mediainfo(path)
        else:  # 文件夹
            test_paths = [
                os.path.join(path, '*.mkv'),
            ]
            for test_path in test_paths:
                test_path_glob = glob(test_path)
                for test_file in test_path_glob:
                    return self._mediainfo(test_file)

    def get_torrent_file(self) -> str:
        """
        因为qbt的API中没有返回种子所在位置，只能通过 'export_dir_fin' 和 'export_dir' 设置值搜索可能存在的种子，
        并通过计算 info_hash 进行确认
        """
        logger.info('正在搜索 %s ( info_hash: %s ) 的种子文件', self.torrent_name, self.info_hash)

        test_paths = [
            os.path.join(self.qbt_preference.get('export_dir_fin'), '{}.torrent'.format(self.torrent_name)),
            os.path.join(self.qbt_preference.get('export_dir'), '{}.torrent'.format(self.torrent_name)),
            os.path.join(self.qbt_preference.get('export_dir_fin'), '*.torrent'),
            os.path.join(self.qbt_preference.get('export_dir'), '*.torrent'),
        ]

        for test_path in test_paths:
            test_path_glob = glob(test_path)
            for test_file in test_path_glob:
                data = bencode.bread(test_file)
                test_info_hash = hashlib.sha1(bencode.encode(data['info'])).hexdigest()
                logger.debug('测试种子 %s ，其info_hash为 %s', test_file, test_info_hash)
                if test_info_hash == self.info_hash:  # 说明该种子文件的info_hash值与想要发布的种子info_hash值相同
                    logger.info('获得种子 "%s" (info_hash: %s) 的种子位置 %s', self.torrent_name, self.info_hash, test_file)
                    self.torrent_file_path = test_file
                    return self.torrent_file_path

        # 说明未搜索到，抛出错误
        raise AutoseedStopException('在种子保存目录中未搜索到 %s (info_hash: %s ) 的种子，你是否未设置 种子保存位置 ??' %
                                    (self.torrent_name, self.info_hash))


if __name__ == '__main__':
    # 实例化 Autoseed 对象
    autoseed = Autoseed()

    try:
        autoseed.run()  # 运行
    except Exception as e:
        logger.error('停止转发: %s', e)

        # 其他错误直接抛出
        if not isinstance(e, AutoseedStopException):
            raise e
