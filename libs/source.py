import re
import sys
import json
import typing

from pathlib import Path
from urllib.parse import unquote
from enum import Enum, auto, unique
from datetime import datetime, timedelta, timezone

import httpx
from lxml import etree
from loguru import logger

from .rsa import RSA  # 外部文件

# 初始化logger
# 如果 logs 文件夹不存在则创建
logDir = Path(__file__).parent.parent / 'logs'
if logDir.exists() is False:
    logDir.mkdir()

# 日志文件用 年-月-日 命名
logFile = Path(logDir / f'{datetime.now().year}-{datetime.now().month}-{datetime.now().day}.log')

# 日志打印、保存。 保存位置、打印格式、颜色、4天清理一次日志
logger.configure(handlers=[
    {
        'sink': sys.stderr,
        'format': '<lvl>{time:YYYY-MM-DD HH:mm:ss.SSS}</> <lvl>|</> <lvl>{message}</>',
        'colorize': True
    },
    {
        'sink': logFile,
        'format': '<lvl>{time:YYYY-MM-DD HH:mm:ss.SSS}</> <lvl>|</> <lvl>{message}</>',
        'colorize': False,
        'retention': '4 days'
    },
])

@unique
class ReturnCode(Enum):
    SUCCESS = auto()
    ALREADY_SIGNED = auto()
    # ALREADY_RESERVED = auto()
    NO_RESERVATION = auto()
    GET_LOGIN_URL_FAILED = auto()
    LOGIN_SKIPED = auto()
    COOKIE_EXPIRED = auto()
    FAILED = auto()

    def __str__(self):
        match self:
            case ReturnCode.SUCCESS:
                return "成功"
            case ReturnCode.ALREADY_SIGNED:
                return "重复签到"
            # case ReturnCode.ALREADY_RESERVED:
            #     return "已预约"
            case ReturnCode.NO_RESERVATION:
                return "没有预约"
            case ReturnCode.GET_LOGIN_URL_FAILED:
                return "获取登录url失败"
            case ReturnCode.LOGIN_SKIPED:
                return "跳过登录"
            case ReturnCode.COOKIE_EXPIRED:
                return "cookie过期"
            case ReturnCode.FAILED:
                return "失败"
            case _:
                return "未知错误"
            
def load_cookie_cache(path: str) -> dict:
    """
    从缓存文件中加载cookie
    :param cookie_path: cookie缓存文件路径
    :return: cookie字典
    """
    
    if Path(path).exists():
        # ic-cookie is a uuid
        uuid_pattern = re.compile(r'[a-f\d]{8}-(?:[a-f\d]{4}-){3}[a-f\d]{12}')
    
        with open(path, 'r', encoding='utf-8') as f:
            cookies = f.readlines()
            cookie_dict = dict()
            for line in cookies:
                line = line.strip().split(None, 1)
                if len(line) != 2:
                    logger.warning(f'[{line[0]}] cookie为空')
                    continue
                if not re.fullmatch(uuid_pattern, line[1]):
                    logger.warning(f'[{line[0]}] 不是合法的cookie: {line[1]} ')
                    continue

                # cookie_dict[name] = ic_cookie
                cookie_dict[line[0]] = line[1]

        return cookie_dict
    else:
        logger.warning(f'cookie缓存文件不存在, 路径: {path}')
        return dict()

def save_cookie_cache(path: str, cookies: dict):
    with open(path, 'w+', encoding='utf-8') as f:
        for name, cookie in cookies.items():
            f.write(f'{name} {cookie}\n')
    logger.info(f'cookie缓存文件已保存到 {path}')

class ZWYT(object):
    def __init__(self, name, username, password, periods, pushplus_token, *, cookie = ''):
        self.resvDev = None  # 座位编号
        self.roomId = None
        self.cookies = {'ic-cookie': cookie}  # 保存登录用的 cookie
        self.name = name  # 名字
        self.username = str(username)  # 学号
        self.password = str(password)  # 密码
        self.periods = periods  # 预约时间段
        self.pushplus_token = pushplus_token  # pushplus 的 token

        # url接口
        self.urls = {
            # 登录
            'login_url': '',  
            # 预约
            'reserve': 'http://libbooking.gzhu.edu.cn/ic-web/reserve',  
            # 获取 roomId
            'seatmenu': 'http://libbooking.gzhu.edu.cn/ic-web/seatMenu',  
            'findaddress': 'http://libbooking.gzhu.edu.cn/ic-web/auth/address',
            'get_location': 'http://libbooking.gzhu.edu.cn/authcenter/toLoginPage',
            # 获取用户信息
            'userinfo': 'http://libbooking.gzhu.edu.cn/ic-web/auth/userInfo',  

            # 获取预约信息
            'resvinfo': 'https://libbooking.gzhu.edu.cn/ic-web/reserve/resvInfo?beginDate={date:%Y-%m-%d}&endDate={date:%Y-%m-%d}&needStatus={needStatus}',
            # request url:
            # https://libbooking.gzhu.edu.cn/ic-web/reserve/resvInfo?
            #   * beginDate=2025-02-17& // 查询开始日期 (必填)
            #   * endDate=2025-02-23&   // 查询结束日期 (必填)
            #   * needStatus=6&         // 查询预约的状态 (非必填), 为并集查询: 如6 = 2 + 4 则查询待生效和已生效的预约
            #       1预约成功 2待生效 4已生效 16已违约 64已签到 128已结束 256待审核 512审核未通过 1024审核通过 2048已暂离 8192待同意 16384举报
            #   (非必填)
            #   * page=1&               // 页号
            #   * pageNum=10&           // 页大小
            #   * orderKey=gmt_create&  // 排序相关?
            #   * orderModel=desc       // 排序相关?

            # 结束正在进行的预约(api确实是endAhaed, 乐)
            'end_ahead': 'https://libbooking.gzhu.edu.cn/ic-web/reserve/endAhaed', 
            # 删除预约
            'delete': 'https://libbooking.gzhu.edu.cn/ic-web/reserve/delete',
            # pushplus
            'pushplus': 'http://www.pushplus.plus/send'
        }

        # xpath 匹配规则
        self.xpath_rules = {
            'lt': '//input[@id="lt"]/@value',
            'execution': '//input[@name="execution"]/@value'
        }

        # 请求头
        self.headers = {
            "Host": "libbooking.gzhu.edu.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36 Edg/107.0.1418.42",
            "token": "a50b1863a0394feab1e4de8d3f370c97",
            "Origin": "http://libbooking.gzhu.edu.cn",
            "Referer": "http://libbooking.gzhu.edu.cn/",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive"
        }

        # 初始化请求连接对象
        self.rr = httpx.Client()



    # pushplus 推送消息到微信
    def pushplus(self, title, content):
        """
        pushplus 推送消息到微信
        Args:
            title: 标题
            content: 消息内容

        Returns: 无
        """
        params = {
            'token': self.pushplus_token,
            "title": title,
            "content": content
        }
        self.rr.get(url=self.urls['pushplus'], params=params)

    # TODO: 整理请求为一个函数
    def get_response(self, url, method, params, headers, data):
        """
        发起请求, 获取响应
        url:
        method:
        params:
        headers:
        data:
        返回数据:
        """
        ...

    # TODO: 请求方式获取 roomId
    def get_roomId(self):
        """
        获取 roomId
        :return:
        """
        res = self.rr.get(url=self.urls['roomId'], headers=self.headers)
        res = res.json()

    # TODO: 请求方式获取 devId
    def get_devId(self):
        params = {
            "roomIds": "100647013",
            "resvDates": "20230514",
            "sysKind": "8"
        }

        res = self.rr.get(url=self.urls['reserve'], params=params, headers=self.headers, timeout=60)
        json_data = res.json()

    # 取对应座位的 resvDev、devSn
    def get_seat_resvDev_devSn(self, devName: str, tag: str):
        """
        tag: 用于判断是预约还是签到。预约需要去json文件获取resvId、签到需要去json文件获取devSn
        devName: 座位编号. 比如 101-011、202-030、3c-011、3c-212、M301-001
        """
        resvDev = None
        filename = devName.strip().split('-')[0]  # 移除传入的座位名头尾的空格后再分割传入的座位名称

        # 预约的是琴房
        if filename[0] == 'M':
            json_path = Path().cwd() / 'json/琴房.json'  # 准备打开的 json 文件的路径
        else:
            json_path = Path().cwd() / f'json/{filename.lower()}.json'  # 准备打开的 json 文件的路径, 先用小写

            if json_path.exists() is False:
                json_path = Path().cwd() / f'json/{filename.upper()}.json'  # 准备打开的 json 文件的路径, 再用大写

        # 打开对应的 json 文件
        with open(json_path, mode='r', encoding='utf-8') as f:
            json_data = json.load(f)

        # 遍历获取对应座位的 devId
        for i in json_data.get('data'):
            if i.get('devName') == devName.upper() or i.get('devName') == devName.lower():
                if tag == 'reserve':  # 预约--去json文件获取resvId
                    resvDev = i.get('devId')
                elif tag == 'sign':  # 签到--去json文件获取devSn
                    resvDev = i.get('devSn')

        return resvDev

    # 获取用户 appAccNo
    def get_person_appAccNo(self):
        """
        获取用户的 appAccNo
        """
        # 请求接口
        res = self.rr.get(url=self.urls['userinfo'], cookies=self.cookies, timeout=60)
        return res.json().get('data').get('accNo')

    def passwordReset(self):
       """
       密码重置
       """
       ...

    # 登录
    def login(self, force_login: bool = False) -> ReturnCode:
        """
        登录
        """
        if not force_login and self.cookies['ic-cookie']:
            logger.info(f'[{self.name}] ic-cookie已存在, 跳过登录')
            return ReturnCode.LOGIN_SKIPED

        try: 
            self.get_login_url()
        except Exception as e:
            logger.error(f'[{self.name}] 获取登录url失败: {e}')
            return ReturnCode.GET_LOGIN_URL_FAILED

        res = self.rr.get(url=self.urls['login_url'], timeout=60)  # 请求登录url获取一些参数
        html = etree.HTML(res.text)

        lt = html.xpath(self.xpath_rules['lt'])[0]
        execution = html.xpath(self.xpath_rules['execution'])[0]
        rsa = RSA().strEnc(self.username + self.password + lt)  # 把密码和那些参数用RSA加密

        data = {
            'rsa': rsa,
            'ul': len(self.username),
            'pl': len(self.password),
            'lt': lt,
            'execution': execution,
            '_eventId': 'submit',
        }
        url = self.urls['login_url']
        res = self.rr.post(url=url, data=data, timeout=60)

        if re.findall('密码重置', res.text):
            self.passwordReset()

        location = str(res.headers.get('Location'))
        ticket = re.findall('ticket=(.*)', location)[0]  # 获取ticket

        url = f"""{re.findall('service=(.*)', url)[0]}?ticket={ticket}"""
        url = unquote(url)
        location = self.rr.get(url=url, timeout=60).headers.get('Location')
        location = unquote(location)

        unitoken = re.findall('uniToken=(.*)', str(location))[0]  # 获取unitoken
        uuid = re.findall('uuid=(.*?)&', str(location))[0]  # 获取 uuid
        params = {
            "manager": "false",
            "uuid": uuid,
            "consoleType": "16",
            "uniToken": unitoken
        }

        # 获取 ic-cookie
        get_cookie_res = self.rr.get(
            url="http://libbooking.gzhu.edu.cn/ic-web//auth/token",
            params=params,
            headers=self.headers,
            timeout=60
        )

        icc = get_cookie_res.headers.get('Set-Cookie')
        self.cookies['ic-cookie'] = re.findall('ic-cookie=(.*?);', icc)[0]

        return ReturnCode.SUCCESS

    #  获取登录url
    def get_login_url(self):
        """
        获取登录带参数的 登录 url
        """
        params = {
            "finalAddress": "http://libbooking.gzhu.edu.cn",
            "errPageUrl": "http://libbooking.gzhu.edu.cn/#/error",
            "manager": "false",
            "consoleType": "16"
        }

        # 从data里面获取一个url
        url = self.urls['findaddress']
        address = self.rr.get(url=url, params=params, timeout=60).json().get('data')

        # 将上面获取到的url 作为请求参数
        url = url = f"{self.urls['get_location']}?redirectUrl={address}"
        res = self.rr.get(url=url, timeout=60)

        self.urls['login_url'] = res.headers.get('Location')

    # 获取预约日期
    def get_reserve_date(self) -> typing.List:
        """
        功能: 返回预约的日期和时间
        return: 返回一个列表, 列表里面每个元素是一个字典, 字典里面有每天的 start(开始时间) 和 end(结束时间)
        """
        utc_now = datetime.utcnow().replace(tzinfo=timezone.utc)  # UTC 时间
        SHA_TZ = timezone(timedelta(hours=8), name='Asia/Shanghai', )  # 上海市区, 也就是东八区，比 UTC 快 8 个小时

        current_day = utc_now.astimezone(SHA_TZ)  # 今天的日期： 北京时间
        next_day = current_day + timedelta(days=1)  # 明天的日期

        # 获取 年、月、日
        c_year, c_month, c_day = current_day.year, current_day.month, current_day.day
        n_year, n_month, n_day = next_day.year, next_day.month, next_day.day

        # 要返回的数据
        reserve_todays = []
        reserve_tomorrows = []

        # 添加起始和结束时间
        for period in self.periods:
            reserve_todays.append(
                {
                    'start': f"{c_year}-{c_month}-{c_day} {period[0]}",  # 今天--起始时间
                    'end': f"{c_year}-{c_month}-{c_day} {period[-1]}"  # 今天--结束时间
                }
            )
            reserve_tomorrows.append(
                {
                    'start': f"{n_year}-{n_month}-{n_day} {period[0]}",  # 明天--起始时间
                    'end': f"{n_year}-{n_month}-{n_day} {period[-1]}"  # 明天--结束时间
                },
            )

        return reserve_tomorrows + reserve_todays
    
    # 查询正在进行并需要签到的预约
    def get_ahead_reservation(self):
        utc_now = datetime.utcnow().replace(tzinfo=timezone.utc)  # UTC 时间
        SHA_TZ = timezone(timedelta(hours=8), name='Asia/Shanghai', )  # 上海市区, 也就是东八区，比 UTC 快 8 个小时
        current_day = utc_now.astimezone(SHA_TZ)  # 今天的日期： 北京时间

        # 4: 已生效的预约
        needStatus = 4
        logger.info(f'[{self.name}] 查询正在进行的预约, 当前日期: {current_day:%Y-%m-%d}')
        url = self.urls['resvinfo'].format(date=current_day, needStatus=4)
        # logger.info(f'[{self.name}] 查询url: {url}')

        res = self.rr.get(
            url=url, 
            cookies=self.cookies, 
            timeout=15
        ) # 4: 已生效的预约
        res = res.json()
        resv = []
        
        code = res.get('code')
        match code:
            case 0:
                for item in res.get('data'):
                    resvStatus = item.get('resvStatus')
                    # 判断是否已签到: 
                    # 不必判断: 1预约成功, 1024审核通过
                    
                    # (not 64已签到 || 2048已暂离)
                    if (resvStatus & 64 == 0) or (resvStatus & 2048 != 0):
                        resv.append(item)
            case 300:
                # ic-cookie 已失效, 需要重新登录
                logger.warning(f'[{self.name}] ic-cookie已失效, 需要重新登录')
                return None, ReturnCode.COOKIE_EXPIRED
            case _:
                logger.error(f'[{self.name}] 查询失败: {res.get("message")}, 状态码: {code}')
                return None, ReturnCode.FAILED

        total_resv = res.get("count")
        logger.info(f'[{self.name}] 查询到预约项: {total_resv - len(resv)}(已预约)/{total_resv}(已生效)')
        if len(resv) == 0:
            logger.info(f'[{self.name}] 没有查询到需要签到的预约')
            return None, ReturnCode.NO_RESERVATION
        elif len(resv) > 1:
            logger.warning(f'[{self.name}] 查询到了多个正在进行的预约: {resv}')

        return resv[0], ReturnCode.SUCCESS

    def sign_for_ahead_reservation(self) -> ReturnCode:
        """
        为正在进行的预约签到
        """

        resv, errc = self.get_ahead_reservation()

        if errc != ReturnCode.SUCCESS:
            return errc
        
        devName = resv.get('resvDevInfoList')[0].get('devName')
        return self.sign(devName)

    # 预约
    def reserve(self, devName: str):
        """
        预约
        """
        # 获取所预约的座位编号
        self.resvDev = self.get_seat_resvDev_devSn(devName, 'reserve')

        # 获取用户的 appAccNo
        appAccNo = self.get_person_appAccNo()

        print('\n')  # 换行

        # 遍历所有日期, 进行预约
        for date in self.get_reserve_date():
            json_data = {
                "sysKind": 8,
                "appAccNo": appAccNo,
                "memberKind": 1,
                "resvMember": [appAccNo],  # 读者个人编号
                "resvBeginTime": date['start'],  # 预约起始时间
                "resvEndTime": date['end'],  # 预约结束时间
                "testName": "",
                "captcha": "",
                "resvProperty": 0,
                "resvDev": [self.resvDev],  # 座位编号
                "memo": ""
            }

            # 发起预约请求
            res = self.rr.post(url=self.urls['reserve'], headers=self.headers, json=json_data, cookies=self.cookies, timeout=60)

            # 将服务器返回数据解析为 json
            res_json = res.json()
            message = res_json.get('message')

            # 预约成功
            if message == '新增成功':
                logger.success(f"[{self.name}] 预约成功: 预约了 {devName}: {json_data['resvBeginTime']} ~ {json_data['resvEndTime']}" )

            # 该时间段有预约了
            elif re.findall('当前时段有预约', message):
                logger.warning(f"[{self.name}] 这个时段已经有了预约: {json_data['resvBeginTime']} ~ {json_data['resvEndTime']}")

            # 预约失败---可选择向微信推送预约失败的信息, 比如可以使用 pushplus 平台
            else:
                logger.error(f"[{self.name}] 时间段: {json_data['resvBeginTime']} 预约失败 {message}")

    # 签到
    def sign(self, devName: str) -> ReturnCode:
        """
        签到
        """

        lurl = "http://libbooking.gzhu.edu.cn/ic-web/phoneSeatReserve/login"
        url = "http://libbooking.gzhu.edu.cn/ic-web/phoneSeatReserve/sign"

        # 获取签到用的 devSn
        devSn = self.get_seat_resvDev_devSn(devName, 'sign')

        # 登录
        res1 = self.rr.post(url=lurl,
                            json={"devSn": devSn, "type": "1", "bind": 0, "loginType": 2},
                            cookies=self.cookies, timeout=60)

        # 返回的json数据
        res1_data = res1.json()

        # 预约座位的编号不对
        if res1_data.get('data') is None:
            logger.warning(f"[{self.name}] {res1_data.get('message')}")

            # 预约的不是当前设备, 则签到对应的座位
            devName = re.findall("-(.*)处", res1_data.get('message'))[0]

            # 调用签到函数进行签到，传入预约座位号
            return self.sign(devName)

        # 暂无预约
        if res1_data.get('data').get('reserveInfo') is None:
            return ReturnCode.NO_RESERVATION

        # 获取预约编号
        resvId = res1_data.get('data').get('reserveInfo').get('resvId')

        # 签到接口
        res2 = self.rr.post(
            url=url, json={"resvId": resvId}, cookies=self.cookies, timeout=60)

        # 获取返回的信息
        message = res2.json().get('message')

        # 签到成功
        if message == '操作成功':
            logger.success(f"[{self.name}] 签到成功--{message}")
            return ReturnCode.SUCCESS

        # 已经签到过
        elif message == '用户已签到，请勿重复签到':
            logger.warning(f'[{self.name}] {message}')
            return ReturnCode.ALREADY_SIGNED

        # 签到失败
        else:
            logger.error(f"[{self.name}] 签到失败--{message}")
            return ReturnCode.FAILED