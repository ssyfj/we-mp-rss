"""
Playwright Driver 单元测试

测试 RefCountManager, CleanupStrategy, ProxyConfigParser, SmartWaitStrategy 等核心组件
"""
import unittest
import threading
import time
from driver.playwright_driver import (
    RefCountManager, 
    CleanupStrategy, 
    ProxyConfigParser,
    SmartWaitStrategy,
    PlaywrightController
)


class TestRefCountManager(unittest.TestCase):
    """测试引用计数管理器"""
    
    def setUp(self):
        self.manager = RefCountManager()
    
    def test_increment(self):
        """测试引用计数增加"""
        thread_id = 1
        count = self.manager.increment(thread_id)
        self.assertEqual(count, 1)
        
        count = self.manager.increment(thread_id)
        self.assertEqual(count, 2)
    
    def test_decrement(self):
        """测试引用计数减少"""
        thread_id = 1
        self.manager.increment(thread_id)
        self.manager.increment(thread_id)
        
        count = self.manager.decrement(thread_id)
        self.assertEqual(count, 1)
        
        count = self.manager.decrement(thread_id)
        self.assertEqual(count, 0)
    
    def test_decrement_non_existent(self):
        """测试减少不存在的线程引用计数"""
        thread_id = 999
        count = self.manager.decrement(thread_id)
        self.assertEqual(count, 0)
    
    def test_get(self):
        """测试获取引用计数"""
        thread_id = 1
        self.manager.increment(thread_id)
        count = self.manager.get(thread_id)
        self.assertEqual(count, 1)
    
    def test_has_thread(self):
        """测试检查线程是否存在"""
        thread_id = 1
        self.assertFalse(self.manager.has_thread(thread_id))
        
        self.manager.increment(thread_id)
        self.assertTrue(self.manager.has_thread(thread_id))
        
        self.manager.decrement(thread_id)
        self.assertFalse(self.manager.has_thread(thread_id))
    
    def test_thread_safety(self):
        """测试线程安全性"""
        thread_id = 1
        iterations = 1000
        
        def increment_many():
            for _ in range(iterations):
                self.manager.increment(thread_id)
        
        def decrement_many():
            for _ in range(iterations):
                self.manager.decrement(thread_id)
        
        # 创建多个线程同时操作
        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=increment_many))
            threads.append(threading.Thread(target=decrement_many))
        
        # 启动所有线程
        for t in threads:
            t.start()
        
        # 等待所有线程完成
        for t in threads:
            t.join()
        
        # 最终引用计数应该为0
        count = self.manager.get(thread_id)
        self.assertEqual(count, 0)


class TestProxyConfigParser(unittest.TestCase):
    """测试代理配置解析器"""
    
    def test_parse_simple(self):
        """测试简单代理URL解析"""
        proxy_url = "http://proxy.example.com:8080"
        result = ProxyConfigParser.parse(proxy_url)
        
        self.assertIsNotNone(result)
        self.assertEqual(result["server"], "http://proxy.example.com:8080")
    
    def test_parse_with_auth(self):
        """测试带认证的代理URL解析"""
        proxy_url = "http://user:pass@proxy.example.com:8080"
        result = ProxyConfigParser.parse(proxy_url)
        
        self.assertIsNotNone(result)
        self.assertEqual(result["server"], "http://proxy.example.com:8080")
        self.assertEqual(result["username"], "user")
        self.assertEqual(result["password"], "pass")
    
    def test_parse_with_special_chars(self):
        """测试包含特殊字符的代理URL解析"""
        proxy_url = "http://user%40domain:p%40ss@proxy.example.com:8080"
        result = ProxyConfigParser.parse(proxy_url)
        
        self.assertIsNotNone(result)
        self.assertEqual(result["username"], "user@domain")
        self.assertEqual(result["password"], "p@ss")
    
    def test_parse_invalid(self):
        """测试无效代理URL"""
        proxy_url = "invalid-url"
        
        with self.assertRaises(ValueError):
            ProxyConfigParser.parse(proxy_url)
    
    def test_parse_empty(self):
        """测试空代理URL"""
        result = ProxyConfigParser.parse("")
        self.assertIsNone(result)
    
    def test_mask_for_log(self):
        """测试代理URL脱敏"""
        proxy_url = "http://user:pass@proxy.example.com:8080"
        masked = ProxyConfigParser.mask_for_log(proxy_url)
        
        self.assertEqual(masked, "http://***:***@proxy.example.com:8080")
    
    def test_mask_for_log_no_auth(self):
        """测试无认证的代理URL脱敏"""
        proxy_url = "http://proxy.example.com:8080"
        masked = ProxyConfigParser.mask_for_log(proxy_url)
        
        self.assertEqual(masked, proxy_url)


class TestCleanupStrategy(unittest.TestCase):
    """测试资源清理策略"""
    
    def test_cleanup_with_retry_none(self):
        """测试清理None资源"""
        result = CleanupStrategy.cleanup_with_retry(None)
        self.assertTrue(result)
    
    def test_cleanup_with_retry_success(self):
        """测试成功清理资源"""
        class MockResource:
            def __init__(self):
                self.closed = False
            
            def close(self):
                self.closed = True
        
        resource = MockResource()
        result = CleanupStrategy.cleanup_with_retry(resource, max_retries=1, delay=0.01)
        
        self.assertTrue(result)
        self.assertTrue(resource.closed)
    
    def test_cleanup_with_retry_failure(self):
        """测试清理失败资源"""
        class MockResource:
            def close(self):
                raise Exception("Close failed")
        
        resource = MockResource()
        result = CleanupStrategy.cleanup_with_retry(resource, max_retries=2, delay=0.01)
        
        self.assertFalse(result)
    
    def test_cleanup_all(self):
        """测试清理所有资源"""
        class MockResource:
            def __init__(self, name):
                self.name = name
                self.closed = False
            
            def close(self):
                self.closed = True
        
        page = MockResource("page")
        context = MockResource("context")
        browser = MockResource("browser")
        
        errors = CleanupStrategy.cleanup_all(page, context, browser, None, False)
        
        self.assertEqual(len(errors), 0)
        self.assertTrue(page.closed)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)


class TestPlaywrightController(unittest.TestCase):
    """测试 PlaywrightController"""
    
    def test_init(self):
        """测试初始化"""
        controller = PlaywrightController()
        
        self.assertIsNone(controller.driver)
        self.assertIsNone(controller.browser)
        self.assertIsNone(controller.context)
        self.assertIsNone(controller.page)
        self.assertTrue(controller.isClose)
    
    def test_get_metrics(self):
        """测试获取性能指标"""
        controller = PlaywrightController()
        metrics = controller.get_metrics()
        
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.open_pages, 0)
        self.assertEqual(metrics.open_contexts, 0)
    
    def test_enable_debug_mode(self):
        """测试调试模式"""
        controller = PlaywrightController()
        
        controller.enable_debug_mode(True)
        self.assertTrue(controller._debug_mode)
        
        controller.enable_debug_mode(False)
        self.assertFalse(controller._debug_mode)
    
    def test_mask_proxy_url(self):
        """测试代理URL脱敏"""
        controller = PlaywrightController()
        
        proxy_url = "http://user:pass@proxy.example.com:8080"
        masked = controller._mask_proxy_url(proxy_url)
        
        self.assertEqual(masked, "http://***:***@proxy.example.com:8080")


class TestSmartWaitStrategy(unittest.TestCase):
    """测试智能等待策略"""
    
    def test_analyze_page_static(self):
        """测试静态页面分析"""
        # 创建一个模拟的page对象
        class MockPage:
            def __init__(self):
                self.request_handlers = []
            
            def on(self, event, handler):
                if event == "request":
                    self.request_handlers.append(handler)
        
        page = MockPage()
        strategy = SmartWaitStrategy.analyze_page(page)
        
        # 没有请求时应该返回 domcontentloaded
        self.assertEqual(strategy, "domcontentloaded")


if __name__ == '__main__':
    unittest.main()
