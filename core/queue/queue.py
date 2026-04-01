import queue
import threading
import time
import gc
from typing import Callable, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field
from core.print import print_error, print_info, print_warning, print_success

@dataclass
class TaskRecord:
    """任务执行记录"""
    task_name: str
    start_time: str
    end_time: Optional[str] = None
    duration: Optional[float] = None
    status: str = "running"  # running, completed, failed
    error: Optional[str] = None

@dataclass  
class TaskItem:
    """队列中的任务项"""
    task_name: str
    args: tuple = field(default_factory=tuple)
    kwargs: dict = field(default_factory=dict)
    add_time: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    retry_count: int = 0  # 当前重试次数
    max_retries: int = 3  # 最大重试次数

class TaskQueueManager:
    """任务队列管理器，用于管理和执行排队任务"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, maxsize=0, tag=""):
        """单例模式：确保只有一个全局实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, maxsize=0, tag=""):
        """初始化任务队列"""
        if self._initialized:
            return
        self._initialized = True
        self._queue = queue.Queue(maxsize=maxsize)
        self._thread_lock = threading.Lock()
        self._is_running = False
        self.tag = tag
        # 任务历史记录（最近100条）
        self._history: list[TaskRecord] = []
        self._history_max_size = 100
        # 当前执行的任务
        self._current_task: Optional[TaskRecord] = None
        # 待执行任务列表（用于展示）
        self._pending_items: list[TaskItem] = []
        print_info(f"TaskQueueManager initialized, instance id: {id(self)}")
        
    def add_task(self, task: Callable[..., Any], *args: Any, max_retries: int = 3, **kwargs: Any) -> bool:
        """添加任务到队列
        
        Args:
            task: 要执行的任务函数
            *args: 任务函数的参数
            max_retries: 最大重试次数，默认3次
            **kwargs: 任务函数的关键字参数
            
        Returns:
            bool: 是否成功添加到队列
        """
        from core.print import print_info
        print_info(f"add_task called, instance id: {id(self)}")
        with self._thread_lock:
            # 检查队列是否已满（如果设置了maxsize）
            try:
                self._queue.put_nowait((task, args, kwargs, max_retries))
            except queue.Full:
                print_error(f"{self.tag}队列已满，任务添加失败")
                return False
                
            # 记录待执行任务
            task_name = getattr(task, '__name__', str(task))
            self._pending_items.append(TaskItem(
                task_name=task_name,
                args=args,
                kwargs=kwargs,
                max_retries=max_retries
            ))
        print_success(f"{self.tag}队列任务添加成功\n")
        return True
    def run_task_background(self)->None:
        threading.Thread(target=self.run_tasks, daemon=True).start()  
        print_warning(f"队列任务后台运行, instance id: {id(self)}")
    def run_tasks(self, timeout: float = 1.0) -> None:
        """执行队列中的所有任务，并持续运行以接收新任务
        
        Args:
            timeout: 等待新任务的超时时间(秒)
        """
        with self._thread_lock:
            if self._is_running:
                return
            self._is_running = True
            
        try:
            while self._is_running:
                time.sleep(0.1)  # 避免过于频繁的任务获取 
                try:
                    # 阻塞获取任务，避免CPU空转
                    task_item = self._queue.get(timeout=timeout)
                    
                    # 兼容新旧格式：新格式4个元素，旧格式3个元素
                    if len(task_item) == 4:
                        task, args, kwargs, max_retries = task_item
                    else:
                        task, args, kwargs = task_item
                        max_retries = 3
                    
                    # 从待执行列表中移除
                    with self._thread_lock:
                        if self._pending_items:
                            self._pending_items.pop(0)
                    
                    # 记录任务开始
                    task_name = getattr(task, '__name__', str(task))
                    with self._thread_lock:
                        self._current_task = TaskRecord(
                            task_name=task_name,
                            start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        )
                    
                    retry_count = 0
                    success = False
                    last_error = None
                    start_time = time.time()  # 提前定义，确保异常时可用
                    
                    while retry_count <= max_retries and not success:
                        if retry_count > 0:
                            print_warning(f"任务 [{task_name}] 第 {retry_count} 次重试...")
                            time.sleep(2 ** retry_count)  # 指数退避
                            
                        try:
                            # 记录任务开始时间
                            start_time = time.time()
                            task(*args, **kwargs)
                            # 记录任务执行时间
                            duration = time.time() - start_time
                            
                            # 更新当前任务记录
                            with self._thread_lock:
                                if self._current_task:
                                    self._current_task.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    self._current_task.duration = duration
                                    self._current_task.status = "completed"
                            
                            print_info(f"\n任务执行完成，耗时: {duration:.2f}秒")
                            success = True
                            
                        except Exception as e:
                            last_error = e
                            retry_count += 1
                            
                            if retry_count <= max_retries:
                                print_warning(f"任务 [{task_name}] 执行失败: {e}，准备重试 ({retry_count}/{max_retries})")
                            else:
                                # 达到最大重试次数，记录失败
                                with self._thread_lock:
                                    if self._current_task:
                                        self._current_task.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                        self._current_task.duration = time.time() - start_time
                                        self._current_task.status = "failed"
                                        self._current_task.error = str(e)
                                
                                print_error(f"任务 [{task_name}] 执行失败，已达到最大重试次数: {e}")
                    
                    # 保存到历史记录
                    with self._thread_lock:
                        if self._current_task:
                            self._history.append(self._current_task)
                            # 限制历史记录大小
                            if len(self._history) > self._history_max_size:
                                self._history = self._history[-self._history_max_size:]
                            self._current_task = None
                    
                    # 确保任务完成标记和资源释放
                    self._queue.task_done()
                    # 强制垃圾回收
                    gc.collect()
                    
                except queue.Empty:
                    # 超时无任务，继续检查运行状态
                    continue
                    
        finally:
            # 确保停止状态设置和资源清理
            with self._thread_lock:
                self._is_running = False
            # 清理可能残留的资源
            gc.collect()
    
    def stop(self) -> None:
        """停止任务执行"""
        with self._thread_lock:
            self._is_running = False
    
    def get_queue_info(self) -> dict:
        """
        获取队列的当前状态信息
        
        返回:
            dict: 包含队列信息的字典，包括:
                - is_running: 队列是否正在运行
                - pending_tasks: 等待执行的任务数量
        """
        with self._thread_lock:
            return {
                'is_running': self._is_running,
                'pending_tasks': self._queue.qsize()
            }
    
    def get_detailed_status(self) -> dict:
        """
        获取队列的详细状态信息
        
        返回:
            dict: 包含详细队列信息的字典
        """
        from core.print import print_info
        print_info(f"Queue instance id: {id(self)}, pending_items: {len(self._pending_items)}, queue_size: {self._queue.qsize()}")
        with self._thread_lock:
            # 转换待执行任务为可序列化格式
            pending_list = []
            for item in self._pending_items:
                pending_list.append({
                    'task_name': item.task_name,
                    'args': str(item.args) if item.args else '',
                    'kwargs': str(item.kwargs) if item.kwargs else '',
                    'add_time': item.add_time
                })
            
            # 转换历史记录为可序列化格式
            history_list = []
            for record in self._history[-20:]:  # 只返回最近20条
                history_list.append({
                    'task_name': record.task_name,
                    'start_time': record.start_time,
                    'end_time': record.end_time,
                    'duration': round(record.duration, 2) if record.duration else None,
                    'status': record.status,
                    'error': record.error
                })
            
            # 当前执行任务
            current = None
            if self._current_task:
                current = {
                    'task_name': self._current_task.task_name,
                    'start_time': self._current_task.start_time,
                    'status': self._current_task.status
                }
            
            return {
                'tag': self.tag,
                'is_running': self._is_running,
                'pending_count': self._queue.qsize(),
                'pending_tasks': pending_list,
                'current_task': current,
                'history_count': len(self._history),
                'recent_history': history_list
            }
    
    def clear_history(self) -> None:
        """清空任务历史记录"""
        with self._thread_lock:
            self._history.clear()
            print_success("任务历史记录已清空")
            
    def clear_queue(self) -> None:
        """清空队列中的所有任务"""
        with self._thread_lock:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
            self._pending_items.clear()
            print_success("队列已清空")
            
    def delete_queue(self) -> None:
        """删除队列(停止并清空所有任务)"""
        with self._thread_lock:
            self._is_running = False
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
            print_success("队列已删除")

# 创建全局单例
TaskQueue = TaskQueueManager(tag="默认队列")
print_info(f"TaskQueue singleton created, instance id: {id(TaskQueue)}")
TaskQueue.run_task_background()
if __name__ == "__main__":
    def task1():
        print("执行任务1")

    def task2(name):
        print(f"执行任务2，参数: {name}")

    manager = TaskQueueManager()
    manager.add_task(task1)
    manager.add_task(task2, "测试任务")
    manager.run_tasks()  # 按顺序执行任务1和任务2