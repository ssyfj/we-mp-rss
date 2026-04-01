"""任务队列管理API"""
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from typing import Optional
from core.auth import get_current_user_or_ak
from core.queue import TaskQueue
from core.task.task import TaskScheduler
from core.ws_manager import ws_manager
from .base import success_response, error_response
from core.log import logger
import asyncio

router = APIRouter(prefix="/task-queue", tags=["任务队列"])

@router.get("/status", summary="获取任务队列状态")
async def get_queue_status(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    获取任务队列的详细状态信息
    
    返回:
        - tag: 队列标签
        - is_running: 是否运行中
        - pending_count: 待执行任务数
        - pending_tasks: 待执行任务列表
        - current_task: 当前执行的任务
        - history_count: 历史记录总数
        - recent_history: 最近执行记录
    """
    try:
        status = TaskQueue.get_detailed_status()
        logger.info(f"Queue status: {status}")
        return success_response(data=status)
    except Exception as e:
        logger.error(f"Get queue status error: {str(e)}")
        return error_response(code=500, message=str(e))

@router.get("/history", summary="获取任务执行历史")
async def get_queue_history(
    limit: int = Query(20, ge=1, le=100, description="返回记录数量"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    获取任务执行历史记录
    
    参数:
        limit: 返回记录数量，默认20条
    """
    try:
        status = TaskQueue.get_detailed_status()
        history = status.get('recent_history', [])[:limit]
        return success_response(data={
            'history': history,
            'total': status.get('history_count', 0)
        })
    except Exception as e:
        return error_response(code=500, message=str(e))

@router.post("/clear", summary="清空任务队列")
async def clear_queue(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    清空任务队列中的所有待执行任务
    
    注意: 正在执行的任务不会被中断
    """
    try:
        TaskQueue.clear_queue()
        return success_response(message="队列已清空")
    except Exception as e:
        return error_response(code=500, message=str(e))

@router.post("/history/clear", summary="清空任务历史")
async def clear_history(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    清空任务执行历史记录
    """
    try:
        TaskQueue.clear_history()
        return success_response(message="任务历史已清空")
    except Exception as e:
        return error_response(code=500, message=str(e))

@router.get("/scheduler/status", summary="获取调度器状态")
async def get_scheduler_status(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    获取定时任务调度器的状态信息
    
    返回:
        - running: 调度器是否运行中
        - job_count: 定时任务数量
        - next_run_times: 各任务下次执行时间
    """
    try:
        # 从 jobs.mps 导入调度器实例
        from jobs.mps import scheduler
        status = scheduler.get_scheduler_status()
        logger.info(f"Scheduler status: {status}")
        return success_response(data=status)
    except ImportError as e:
        logger.error(f"Import scheduler error: {str(e)}")
        return success_response(data={
            'running': False,
            'job_count': 0,
            'next_run_times': []
        })
    except Exception as e:
        logger.error(f"Get scheduler status error: {str(e)}")
        return error_response(code=500, message=str(e))

@router.get("/scheduler/jobs", summary="获取定时任务列表")
async def get_scheduler_jobs(
    current_user: dict = Depends(get_current_user_or_ak)
):
    """
    获取所有定时任务的详细信息
    """
    try:
        from jobs.mps import scheduler
        job_ids = scheduler.get_job_ids()
        jobs = []
        for job_id in job_ids:
            try:
                details = scheduler.get_job_details(job_id)
                jobs.append(details)
            except Exception as job_error:
                logger.warning(f"Get job {job_id} details error: {str(job_error)}")
                jobs.append({'id': job_id, 'error': '获取详情失败'})
        logger.info(f"Scheduler jobs: {len(jobs)} jobs")
        return success_response(data={
            'jobs': jobs,
            'total': len(jobs)
        })
    except ImportError as e:
        logger.error(f"Import scheduler error: {str(e)}")
        return success_response(data={
            'jobs': [],
            'total': 0
        })
    except Exception as e:
        logger.error(f"Get scheduler jobs error: {str(e)}")
        return error_response(code=500, message=str(e))


@router.websocket("/ws")
async def queue_websocket(websocket: WebSocket, token: Optional[str] = None):
    """
    WebSocket 端点，实时推送队列状态更新
    
    参数:
        token: JWT 认证令牌（通过查询参数传递）
    
    消息格式:
    {
        "type": "queue_status",
        "data": { ... 队列状态 ... }
    }
    """
    # 验证 token
    if not token:
        await websocket.close(code=4001, reason="未提供认证令牌")
        return
    
    try:
        import jwt
        from core.auth import SECRET_KEY, ALGORITHM, get_user
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            await websocket.close(code=4001, reason="无效的令牌")
            return
        
        user = get_user(username)
        if not user:
            await websocket.close(code=4001, reason="用户不存在")
            return
    except jwt.ExpiredSignatureError:
        await websocket.close(code=4001, reason="令牌已过期")
        return
    except jwt.PyJWTError as e:
        logger.warning(f"WebSocket 认证失败: {e}")
        await websocket.close(code=4001, reason="认证失败")
        return
    except Exception as e:
        logger.error(f"WebSocket 认证异常: {e}")
        await websocket.close(code=4001, reason="认证异常")
        return
    
    await ws_manager.connect(websocket)
    try:
        # 立即发送当前状态
        status = TaskQueue.get_detailed_status()
        await websocket.send_json({
            "type": "queue_status",
            "data": status
        })
        
        # 保持连接，定期推送状态（每3秒）
        while True:
            await asyncio.sleep(3)
            status = TaskQueue.get_detailed_status()
            await websocket.send_json({
                "type": "queue_status",
                "data": status
            })
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
        ws_manager.disconnect(websocket)


async def broadcast_queue_status():
    """广播队列状态到所有 WebSocket 连接"""
    status = TaskQueue.get_detailed_status()
    await ws_manager.broadcast({
        "type": "queue_status",
        "data": status
    })


def broadcast_queue_status_sync():
    """同步版本：广播队列状态"""
    status = TaskQueue.get_detailed_status()
    ws_manager.broadcast_sync({
        "type": "queue_status",
        "data": status
    })
