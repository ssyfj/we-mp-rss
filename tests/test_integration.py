"""
Playwright Driver 集成测试

测试多线程环境下的整体功能
"""
import unittest
import threading
import time
from driver.playwright_driver import PlaywrightController


class TestMultiThreadIntegration(unittest.TestCase):
    """多线程集成测试"""
    
    def test_multi_thread_browser_instances(self):
        """测试多线程环境下每个线程拥有独立的浏览器实例"""
        results = {}
        errors = []
        
        def worker(thread_id):
            try:
                controller = PlaywrightController()
                
                # 记录线程ID
                results[thread_id] = {
                    'thread_id': threading.current_thread().ident,
                    'controller_id': id(controller)
                }
                
                # 模拟浏览器操作(不实际启动,只测试线程隔离)
                time.sleep(0.1)
                
                # 清理
                controller.cleanup()
                
            except Exception as e:
                errors.append(f"Thread {thread_id}: {str(e)}")
        
        # 创建多个线程
        threads = []
        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()
        
        # 等待所有线程完成
        for t in threads:
            t.join()
        
        # 验证结果
        self.assertEqual(len(errors), 0, f"Errors occurred: {errors}")
        self.assertEqual(len(results), 5)
        
        # 验证每个线程都有不同的controller实例
        controller_ids = [r['controller_id'] for r in results.values()]
        self.assertEqual(len(set(controller_ids)), 5, "每个线程应该有独立的controller实例")
    
    def test_ref_count_management(self):
        """测试引用计数管理"""
        controller = PlaywrightController()
        
        # 获取初始引用计数
        thread_id = threading.current_thread().ident
        initial_count = PlaywrightController._ref_count_manager.get(thread_id)
        
        # 模拟多次启动(不实际启动浏览器)
        # 这里只测试引用计数逻辑
        for i in range(3):
            PlaywrightController._ref_count_manager.increment(thread_id)
        
        # 验证引用计数
        count = PlaywrightController._ref_count_manager.get(thread_id)
        self.assertEqual(count, initial_count + 3)
        
        # 减少引用计数
        for i in range(3):
            PlaywrightController._ref_count_manager.decrement(thread_id)
        
        # 验证引用计数回到初始值
        final_count = PlaywrightController._ref_count_manager.get(thread_id)
        self.assertEqual(final_count, initial_count)
    
    def test_metrics_collection(self):
        """测试性能指标收集"""
        controller = PlaywrightController()
        
        # 启用调试模式
        controller.enable_debug_mode(True)
        
        # 获取性能指标
        metrics = controller.get_metrics()
        
        # 验证指标存在
        self.assertIsNotNone(metrics)
        self.assertIsInstance(metrics.browser_startup_time, float)
        self.assertIsInstance(metrics.total_operations, int)
        
        # 禁用调试模式
        controller.enable_debug_mode(False)
    
    def test_proxy_config(self):
        """测试代理配置"""
        controller = PlaywrightController()
        
        # 测试代理URL解析
        proxy_url = "http://user:pass@proxy.example.com:8080"
        proxy_options = controller._build_proxy_options(proxy_url)
        
        self.assertIsNotNone(proxy_options)
        self.assertEqual(proxy_options["server"], "http://proxy.example.com:8080")
        self.assertEqual(proxy_options["username"], "user")
        self.assertEqual(proxy_options["password"], "pass")
        
        # 测试代理URL脱敏
        masked = controller._mask_proxy_url(proxy_url)
        self.assertEqual(masked, "http://***:***@proxy.example.com:8080")


class TestResourceManagement(unittest.TestCase):
    """资源管理测试"""
    
    def test_cleanup_order(self):
        """测试资源清理顺序"""
        cleanup_order = []
        
        class MockResource:
            def __init__(self, name):
                self.name = name
            
            def close(self):
                cleanup_order.append(self.name)
        
        # 创建模拟资源
        page = MockResource("page")
        context = MockResource("context")
        browser = MockResource("browser")
        
        # 使用 CleanupStrategy 清理
        from driver.playwright_driver import CleanupStrategy
        errors = CleanupStrategy.cleanup_all(page, context, browser, None, False)
        
        # 验证清理顺序
        self.assertEqual(len(errors), 0)
        self.assertEqual(cleanup_order, ["page", "context", "browser"])
    
    def test_cleanup_with_exception(self):
        """测试异常情况下的资源清理"""
        from driver.playwright_driver import CleanupStrategy
        
        class FailingResource:
            def close(self):
                raise Exception("Close failed")
        
        class SuccessResource:
            def __init__(self, name):
                self.name = name
                self.closed = False
            
            def close(self):
                self.closed = True
        
        # page 清理会失败,但其他资源应该继续清理
        page = FailingResource()
        context = SuccessResource("context")
        browser = SuccessResource("browser")
        
        errors = CleanupStrategy.cleanup_all(page, context, browser, None, False)
        
        # 验证有错误记录
        self.assertGreater(len(errors), 0)
        
        # 验证其他资源被清理
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)


class TestSmartWaitStrategy(unittest.TestCase):
    """智能等待策略测试"""
    
    def test_wait_strategy_selection(self):
        """测试等待策略选择"""
        from driver.playwright_driver import SmartWaitStrategy
        
        # 创建模拟page对象
        class MockPage:
            def __init__(self, request_count=0):
                self.request_count = request_count
            
            def on(self, event, handler):
                pass
        
        # 测试静态页面(少量请求)
        page = MockPage(request_count=2)
        strategy = SmartWaitStrategy.analyze_page(page)
        self.assertEqual(strategy, "domcontentloaded")


if __name__ == '__main__':
    unittest.main()
