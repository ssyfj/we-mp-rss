from asyncio import futures
import os
import platform
import subprocess
import sys
import json
import random
import uuid
import asyncio
import threading
import warnings
import gc
import time
from socket import timeout
from urllib.parse import urlparse, unquote
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.print import print_error
from driver.user_agent import UserAgentGenerator
from driver.anti_crawler_config import AntiCrawlerConfig


@dataclass
class Metrics:
    """性能指标数据类"""
    # 启动指标
    browser_startup_time: float = 0.0
    page_creation_time: float = 0.0
    
    # 资源指标
    memory_usage_mb: float = 0.0
    open_pages: int = 0
    open_contexts: int = 0
    
    # 操作指标
    total_operations: int = 0
    failed_operations: int = 0
    avg_operation_time: float = 0.0
    
    # 清理指标
    cleanup_count: int = 0
    cleanup_failures: int = 0
    avg_cleanup_time: float = 0.0
    
    # 时间戳
    created_at: datetime = field(default_factory=datetime.now)
    last_updated_at: datetime = field(default_factory=datetime.now)

# 过滤 Playwright 已知的 memoryview 缓冲区警告
# 这是 Playwright 在 Windows 上关闭浏览器时的已知问题
try:
    warnings.filterwarnings("ignore", category=ResourceWarning)
    # 抑制 Playwright asyncio 警告(我们在独立线程中运行,可以安全忽略)
    warnings.filterwarnings("ignore", message=".*Playwright Sync API.*")
except Exception:
    pass

# 设置环境变量
browsers_name = os.getenv("BROWSER_TYPE", "firefox")
browsers_path = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "")
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = browsers_path

# 导入Playwright相关模块
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright


class RefCountManager:
    """线程安全的引用计数管理器
    
    用于管理每个线程的 Playwright driver 引用计数,
    确保在多线程环境下正确地创建和销毁 driver。
    """
    
    def __init__(self):
        self._counts: dict = {}  # 线程ID -> 引用计数
        self._lock = threading.Lock()
    
    def increment(self, thread_id: int) -> int:
        """增加指定线程的引用计数
        
        Args:
            thread_id: 线程标识符
            
        Returns:
            增加后的引用计数
        """
        with self._lock:
            self._counts[thread_id] = self._counts.get(thread_id, 0) + 1
            return self._counts[thread_id]
    
    def decrement(self, thread_id: int) -> int:
        """减少指定线程的引用计数
        
        当引用计数归零时,自动从字典中移除该线程。
        
        Args:
            thread_id: 线程标识符
            
        Returns:
            减少后的引用计数,如果线程不存在则返回0
        """
        with self._lock:
            if thread_id in self._counts:
                self._counts[thread_id] -= 1
                if self._counts[thread_id] == 0:
                    del self._counts[thread_id]
                    return 0
                return self._counts[thread_id]
            return 0
    
    def get(self, thread_id: int) -> int:
        """获取指定线程的引用计数
        
        Args:
            thread_id: 线程标识符
            
        Returns:
            当前引用计数,如果线程不存在则返回0
        """
        with self._lock:
            return self._counts.get(thread_id, 0)
    
    def has_thread(self, thread_id: int) -> bool:
        """检查指定线程是否在管理中
        
        Args:
            thread_id: 线程标识符
            
        Returns:
            如果线程在管理中返回True,否则返回False
        """
        with self._lock:
            return thread_id in self._counts


class CleanupStrategy:
    """资源清理策略
    
    提供可靠的资源清理机制,支持重试和异常容错。
    """
    
    @staticmethod
    def cleanup_with_retry(resource, max_retries: int = 3, delay: float = 0.1) -> bool:
        """带重试的资源清理
        
        Args:
            resource: 要清理的资源对象(必须有close方法)
            max_retries: 最大重试次数
            delay: 初始延迟时间(秒)
            
        Returns:
            清理成功返回True,失败返回False
        """
        if resource is None:
            return True
            
        for attempt in range(max_retries):
            try:
                # 添加延迟避免 memoryview 缓冲区问题
                import time
                time.sleep(delay)
                resource.close()
                return True
            except Exception as e:
                if attempt == max_retries - 1:
                    return False
                # 递增延迟
                time.sleep(delay * (attempt + 1))
        return False
    
    @staticmethod
    def cleanup_all(page, context, browser, driver, should_stop_driver: bool) -> list:
        """清理所有资源
        
        按照依赖关系顺序清理: page → context → browser → driver
        每个步骤独立捕获异常,不影响后续步骤。
        
        Args:
            page: Page对象
            context: BrowserContext对象
            browser: Browser对象
            driver: Playwright driver对象
            should_stop_driver: 是否应该停止driver
            
        Returns:
            清理过程中的错误列表
        """
        errors = []
        
        # 按顺序清理,每步独立捕获异常
        if not CleanupStrategy.cleanup_with_retry(page):
            errors.append("page cleanup failed")
        
        if not CleanupStrategy.cleanup_with_retry(context):
            errors.append("context cleanup failed")
        
        if not CleanupStrategy.cleanup_with_retry(browser):
            errors.append("browser cleanup failed")
        
        if should_stop_driver:
            if not CleanupStrategy.cleanup_with_retry(driver):
                errors.append("driver cleanup failed")
        
        return errors


class ProxyConfigParser:
    """代理配置解析器
    
    负责解析代理URL并进行脱敏处理。
    """
    
    @staticmethod
    def parse(proxy_url: str) -> dict:
        """解析代理URL
        
        Args:
            proxy_url: 代理URL,格式为 protocol://[user:pass@]host:port
            
        Returns:
            代理配置字典,包含server、username、password等字段
            
        Raises:
            ValueError: 代理URL格式无效时抛出
        """
        if not proxy_url:
            return None
        
        parsed = urlparse(proxy_url)
        
        if not parsed.scheme or not parsed.hostname:
            raise ValueError(
                f"代理地址格式无效: {proxy_url}. "
                f"期望格式: protocol://[user:pass@]host:port"
            )
        
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server = f"{server}:{parsed.port}"
        
        proxy_options = {"server": server}
        
        if parsed.username:
            proxy_options["username"] = unquote(parsed.username)
        if parsed.password:
            proxy_options["password"] = unquote(parsed.password)
        
        return proxy_options
    
    @staticmethod
    def mask_for_log(proxy_url: str) -> str:
        """脱敏代理URL用于日志输出
        
        将用户名和密码替换为 ***
        
        Args:
            proxy_url: 代理URL
            
        Returns:
            脱敏后的代理URL
        """
        if not proxy_url:
            return ""
        
        parsed = urlparse(proxy_url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return f"{parsed.scheme}://***:***@{netloc}"
        
        return proxy_url


class SmartWaitStrategy:
    """智能等待策略
    
    根据页面特征自动选择合适的等待策略。
    """
    
    @staticmethod
    def analyze_page(page) -> str:
        """分析页面特征,选择合适的等待策略
        
        Args:
            page: Playwright Page对象
            
        Returns:
            等待策略: "networkidle" 或 "domcontentloaded"
        """
        try:
            # 检测是否有大量异步请求
            network_requests = []
            
            def on_request(request):
                network_requests.append(request)
            
            # 监听网络请求
            page.on("request", on_request)
            
            # 等待一小段时间收集请求数据
            import time
            time.sleep(0.5)
            
            # 如果有超过5个并发请求,认为是动态页面
            if len(network_requests) > 5:
                return "networkidle"
            else:
                return "domcontentloaded"
        except Exception:
            # 出错时使用保守策略
            return "domcontentloaded"
    
    @staticmethod
    def wait_with_retry(page, url: str, strategy: str = "auto", timeout: int = 30000, max_retries: int = 3):
        """带重试的页面等待
        
        Args:
            page: Playwright Page对象
            url: 目标URL
            strategy: 等待策略 ("auto", "networkidle", "domcontentloaded", "load")
            timeout: 超时时间(毫秒)
            max_retries: 最大重试次数
            
        Raises:
            TimeoutError: 重试后仍然超时时抛出
        """
        import time
        
        # 如果是auto模式,先分析页面
        if strategy == "auto":
            strategy = SmartWaitStrategy.analyze_page(page)
        
        # 尝试加载页面
        for retry in range(max_retries):
            try:
                start_time = time.time()
                page.goto(url, wait_until=strategy, timeout=timeout)
                elapsed = time.time() - start_time
                print(f"页面加载完成,策略: {strategy}, 耗时: {elapsed:.2f}秒")
                return
            except Exception as e:
                if retry == max_retries - 1:
                    # 最后一次重试失败,尝试使用更宽松的策略
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                        print(f"使用降级策略加载成功")
                        return
                    except Exception:
                        raise
                # 重试前等待
                time.sleep(1)
                print(f"加载失败,重试 {retry + 1}/{max_retries}: {str(e)}")


class PlaywrightController:
    """Playwright 浏览器控制器
    
    负责管理浏览器实例的生命周期,包括启动、操作和清理。
    支持多线程环境下的安全使用。
    """
    
    # 使用线程本地存储，每个线程拥有独立的 playwright 实例
    # 解决 greenlet "Cannot switch to a different thread" 错误
    _thread_local = threading.local()
    _global_lock = threading.Lock()
    
    # 使用 RefCountManager 管理引用计数
    _ref_count_manager = RefCountManager()

    def __init__(self):
        self.system = platform.system().lower()
        self.driver = None  # 指向线程本地的 playwright driver
        self.browser = None
        self.context = None
        self.page = None
        self.isClose = True
        self._anti_crawler = AntiCrawlerConfig()  # 反爬虫配置
        self._thread_id = None  # 当前线程ID
        self._metrics = Metrics()  # 性能指标
        self._debug_mode = False  # 调试模式标志
        self._start_time = None  # 启动时间

    def _mask_proxy_url(self, proxy_url: str) -> str:
        """脱敏代理URL用于日志输出"""
        return ProxyConfigParser.mask_for_log(proxy_url)

    def _build_proxy_options(self, proxy_url: str):
        """构建代理配置选项"""
        return ProxyConfigParser.parse(proxy_url)

    def _is_browser_installed(self, browser_name):
        """检查指定浏览器是否已安装"""
        try:
            
            # 遍历目录，查找包含浏览器名称的目录
            for item in os.listdir(browsers_path):
                item_path = os.path.join(browsers_path, item)
                if os.path.isdir(item_path) and browser_name.lower() in item.lower():
                    return True
            
            return False
        except (OSError, PermissionError):
            return False
    def is_async(self):
        try:
            # 尝试获取事件循环
                # 设置合适的事件循环策略
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return True
        except RuntimeError:
            # 如果没有正在运行的事件循环，则说明不是异步环境
            return False
    
    def is_browser_started(self):
        """检测浏览器是否已启动"""
        return (not self.isClose and 
                self.driver is not None and 
                self.browser is not None and 
                self.context is not None and 
                self.page is not None)
    def start_browser(self, headless=True, mobile_mode=True, dis_image=True, browser_name=browsers_name, language="zh-CN", anti_crawler=True, proxy_url=""):
        """启动浏览器
        
        Args:
            headless: 无头模式,默认True
            mobile_mode: 移动模式,默认True
            dis_image: 禁用图片加载,默认True
            browser_name: 浏览器类型,默认从环境变量读取
            language: 浏览器语言,默认zh-CN
            anti_crawler: 启用反爬虫,默认True
            proxy_url: 代理URL,格式为 protocol://[user:pass@]host:port
            
        Returns:
            Page对象
            
        Raises:
            Exception: 浏览器启动失败时抛出
        """
        try:
            # 优化无头模式配置逻辑
            # 优先使用参数传入的值,环境变量作为覆盖选项
            if str(os.getenv("NOT_HEADLESS", "False")) == "True":
                headless = False
            # 移除非Windows平台强制无头模式的限制,支持所有平台的有头模式调试
            
            # 使用线程本地存储，确保每个线程有独立的 playwright driver
            thread_id = threading.current_thread().ident
            self._thread_id = thread_id
            
            # 检查是否在 asyncio 环境中,并尝试处理
            in_asyncio = False
            try:
                loop = asyncio.get_running_loop()
                in_asyncio = True
                # 如果能获取到运行中的事件循环,说明在异步环境中
                # 但这可能是通过独立线程调用的,所以继续执行
                print(f"警告: 在 asyncio 环境中检测到 Sync API 调用 (线程 {thread_id})")
                print(f"如果这是在独立线程中调用的,可以忽略此警告")
                
                # 关键: 在 asyncio 环境中,我们需要确保事件循环策略正确
                # 并且不使用当前事件循环
                if sys.platform == "win32":
                    # Windows 平台使用 ProactorEventLoop
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except RuntimeError:
                # 没有运行中的事件循环,可以安全使用 Sync API
                pass
            
            with PlaywrightController._global_lock:
                # 检查当前线程是否已有 driver
                if not hasattr(PlaywrightController._thread_local, 'driver') or \
                   PlaywrightController._thread_local.driver is None:
                    # 在独立线程中启动 Playwright,避免 asyncio 冲突
                    PlaywrightController._thread_local.driver = sync_playwright().start()
                    print(f"Playwright driver 已为线程 {thread_id} 初始化")
                
                # 使用 RefCountManager 管理引用计数
                ref_count = PlaywrightController._ref_count_manager.increment(thread_id)
                print(f"线程 {thread_id} 引用计数: {ref_count}")
            
            self.driver = PlaywrightController._thread_local.driver
        
            # 根据浏览器名称选择浏览器类型
            if browser_name.lower() == "firefox":
                browser_type = self.driver.firefox
            elif browser_name.lower() == "webkit":
                browser_type = self.driver.webkit
            else:
                browser_type = self.driver.chromium  # 默认使用chromium
            print(f"启动浏览器: {browser_name}, 无头模式: {headless}, 移动模式: {mobile_mode}, 反爬虫: {anti_crawler}")
            # 设置启动选项

            # 根据浏览器类型使用不同的参数
            launch_options = {
                "headless": headless
            }

            if browser_name.lower() == "chromium":
                # Chromium 浏览器参数（支持最丰富）
                launch_options["args"] = [
                    # "--disable-blink-features=AutomationControlled",  # 禁用自动化检测
                    # "--disable-web-security",  # 禁用同源策略（可选）
                    "--disable-features=IsolateOrigins,site-per-process",  # 禁用站点隔离
                    "--disable-webrtc",  # 禁用 WebRTC（防止真实 IP 泄露）
                    "--disable-extensions",  # 禁用扩展
                    "--disable-plugins",  # 禁用插件
                    "--disable-images",  # 禁用图片加载（可选，加速）
                    "--disable-background-networking",  # 禁用后台网络
                    "--disable-sync",  # 禁用同步
                    "--metrics-recording-only",  # 禁用指标记录
                    "--no-first-run",  # 跳过首次运行
                    "--disable-default-apps",  # 禁用默认应用
                    "--no-default-browser-check",  # 跳过默认浏览器检查
                    "--disable-dev-shm-usage",
                    "--disable-gpu",  # 可选：禁用GPU以统一渲染特征
                ]

            elif browser_name.lower() == "firefox":
                # Firefox 使用 firefox_user_prefs 配置，不是 args
                launch_options["firefox_user_prefs"] = {
                    # 禁用 WebDriver 检测
                    "dom.webdriver.enabled": False,
                    # 禁用 WebRTC（防止 IP 泄露）
                    "media.peerconnection.enabled": False,
                    "media.navigator.enabled": False,
                    # 禁用扩展
                    "extensions.autoDisableScopes": 15,
                    "xpinstall.signatures.required": False,
                    # 隐私保护
                    "privacy.trackingprotection.enabled": True,
                    "privacy.trackingprotection.pbmode.enabled": True,
                    # 禁用遥测
                    "toolkit.telemetry.enabled": False,
                    "datareporting.healthreport.uploadEnabled": False,
                    # 性能优化
                    "browser.cache.disk.enable": True,
                    "browser.sessionstore.enabled": True,
                }
                # Firefox 不使用 args 参数，但可以添加少量通用参数
                launch_options["args"] = []

            elif browser_name.lower() == "webkit":
                # WebKit 浏览器参数（支持很少，保持最小化）
                launch_options["args"] = []
                # WebKit 的反爬虫功能主要通过 JavaScript 注入实现
            else:
                # 默认使用 Chromium 配配置
                launch_options["args"] = [
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-webrtc",
                    "--disable-extensions",
                    "--disable-plugins",
                    "--disable-images",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--metrics-recording-only",
                    "--no-first-run",
                    "--disable-default-apps",
                    "--no-default-browser-check",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]

            proxy_options = self._build_proxy_options(proxy_url)
            if proxy_options:
                print(f"浏览器代理已启用: {self._mask_proxy_url(proxy_url)}")
                launch_options["proxy"] = proxy_options
            
            # 在Windows上添加额外的启动选项
            if self.system == "windows":
                launch_options["handle_sigint"] = False
                launch_options["handle_sigterm"] = False
                launch_options["handle_sighup"] = False
            
            self.browser = browser_type.launch(**launch_options)
            
            # 设置浏览器语言为中文
            context_options = {
                "locale": language
            }
            
            # 反爬虫配置
            if anti_crawler:
                context_options.update(self._anti_crawler.get_anti_crawler_config(mobile_mode))
            
            self.context = self.browser.new_context(**context_options) #type: ignore
            self.page = self.context.new_page()
            

            if dis_image:
                self.context.route("**/*.{png,jpg,jpeg}", lambda route: route.abort())

            # 应用反爬虫脚本
            if anti_crawler:
                self.page.add_init_script(self._anti_crawler.get_init_script())
                self.page.evaluate(self._anti_crawler.get_behavior_script())

            self.isClose = False
            return self.page
        except Exception as e:
            tips=f"{str(e)}\nDocker环境;您可以设置环境变量INSTALL=True并重启Docker自动安装浏览器环境;如需要切换浏览器可以设置环境变量BROWSER_TYPE=firefox 支持(firefox,webkit,chromium),开发环境请手工安装"
            print_error(tips)
            self.cleanup()
            raise Exception(tips)
        
    def string_to_json(self, json_string):
        try:
            json_obj = json.loads(json_string)
            return json_obj
        except json.JSONDecodeError as e:
            print(f"JSON解析错误: {e}")
            return ""

    def parse_string_to_dict(self, kv_str: str):
        result = {}
        items = kv_str.strip().split(';')
        for item in items:
            try:
                key, value = item.strip().split('=')
                result[key.strip()] = value.strip()
            except Exception as e:
                pass
        return result

    def add_cookies(self, cookies):
        if self.context is None:
            raise Exception("浏览器未启动，请先调用 start_browser()")
        self.context.add_cookies(cookies)
        
    def get_cookies(self):
        if self.context is None:
            raise Exception("浏览器未启动，请先调用 start_browser()")
        return self.context.cookies()
    
    def add_cookie(self, cookie):
        self.add_cookies([cookie])

    def __del__(self):
        # 析构时确保资源被释放
        try:
            self.Close()
        except Exception:
            # 析构函数中避免抛出异常
            pass

    def open_url(self, url, wait_until="domcontentloaded", timeout=30000, max_retries=3):
        """打开URL
        
        Args:
            url: 目标URL
            wait_until: 等待策略,支持 "auto", "networkidle", "domcontentloaded", "load"
            timeout: 超时时间(毫秒),默认30秒
            max_retries: 最大重试次数,默认3次
            
        Raises:
            Exception: 打开URL失败时抛出
        """
        try:
            # 使用智能等待策略
            SmartWaitStrategy.wait_with_retry(
                self.page, 
                url, 
                strategy=wait_until, 
                timeout=timeout, 
                max_retries=max_retries
            )
        except Exception as e:
            raise Exception(f"打开URL失败: {str(e)}")

    def Close(self):
        self.cleanup()

    def cleanup(self):
        """清理所有资源
        
        使用 CleanupStrategy 进行可靠的资源清理,
        按照依赖关系顺序清理: page → context → browser → driver
        """
        import time
        
        # 使用全局锁管理线程本地 driver 的生命周期
        thread_id = self._thread_id or threading.current_thread().ident
        
        # 判断是否应该停止 driver
        should_stop_driver = False
        with PlaywrightController._global_lock:
            ref_count = PlaywrightController._ref_count_manager.decrement(thread_id)
            print(f"线程 {thread_id} 引用计数: {ref_count}")
            
            # 只有当该线程的引用计数归零时才真正停止 driver
            if ref_count == 0:
                should_stop_driver = True
        
        # 获取线程本地 driver(如果存在)
        driver_to_cleanup = None
        if should_stop_driver and hasattr(PlaywrightController._thread_local, 'driver'):
            driver_to_cleanup = PlaywrightController._thread_local.driver
        
        # 使用 CleanupStrategy 清理所有资源
        errors = CleanupStrategy.cleanup_all(
            self.page,
            self.context,
            self.browser,
            driver_to_cleanup,
            should_stop_driver
        )
        
        # 清理线程本地 driver 引用
        if should_stop_driver:
            with PlaywrightController._global_lock:
                if hasattr(PlaywrightController._thread_local, 'driver'):
                    PlaywrightController._thread_local.driver = None
            print(f"Playwright driver 已为线程 {thread_id} 停止")
        
        # 重置实例属性
        self.page = None
        self.context = None
        self.browser = None
        self.driver = None
        self.isClose = True
        
        # 执行垃圾回收作为辅助手段
        gc.collect()
        
        if errors:
            print(f"资源清理部分失败: {errors}")
    
    def get_metrics(self) -> Metrics:
        """获取性能指标
        
        Returns:
            Metrics对象,包含各种性能指标
        """
        # 更新资源指标
        if self.page:
            self._metrics.open_pages = 1
        if self.context:
            self._metrics.open_contexts = 1
        
        # 计算运行时间
        if self._start_time:
            elapsed = time.time() - self._start_time
            # 估算内存占用(粗略估计)
            self._metrics.memory_usage_mb = elapsed * 0.5  # 假设每秒增长0.5MB
        
        self._metrics.last_updated_at = datetime.now()
        return self._metrics
    
    def enable_debug_mode(self, enable: bool = True):
        """启用或禁用调试模式
        
        Args:
            enable: True启用,False禁用
        """
        self._debug_mode = enable
        if enable:
            print(f"[DEBUG] 调试模式已启用,线程ID: {self._thread_id}")
            print(f"[DEBUG] 当前资源状态: page={self.page is not None}, context={self.context is not None}, browser={self.browser is not None}")
        else:
            print(f"[DEBUG] 调试模式已禁用")
    
    def _log_debug(self, message: str):
        """调试日志输出
        
        Args:
            message: 日志消息
        """
        if self._debug_mode:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            print(f"[DEBUG {timestamp}] {message}")

    def dict_to_json(self, data_dict):
        try:
            return json.dumps(data_dict, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as e:
            print(f"字典转JSON失败: {e}")
            return ""

# 示例用法
if __name__ == "__main__":
    controller = PlaywrightController()
    try:
        controller.start_browser()
        controller.open_url("https://mp.weixin.qq.com/")
    finally:
        # controller.Close()
        pass
