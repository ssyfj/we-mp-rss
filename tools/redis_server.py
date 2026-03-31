#!/usr/bin/env python3
"""
轻量级 Redis 服务端实现
支持基本的 Redis 协议 (RESP) 和常用命令

使用方法:
    python redis_server.py --port 6379 --max-memory 128

支持命令:
    PING, ECHO, SET, GET, DEL, EXISTS, KEYS, TTL, EXPIRE
    INCR, DECR, LPUSH, RPUSH, LPOP, RPOP, LLEN, LRANGE
    HSET, HGET, HDEL, HGETALL, HEXISTS, HKEYS, HVALS
    SADD, SREM, SMEMBERS, SISMEMBER, SCARD
    INFO, DBSIZE, FLUSHDB, FLUSHALL, SAVE, SHUTDOWN
"""

import socket
import threading
import time
import argparse
import json
import os
from typing import Dict, Any, Optional, List, Tuple, Union
from datetime import datetime
from collections import OrderedDict
import signal
import sys


class RedisError(Exception):
    """Redis 错误异常"""
    pass


class RESPParser:
    """Redis 序列化协议 (RESP) 解析器"""
    
    CRLF = b'\r\n'
    
    @staticmethod
    def parse(data: bytes) -> Tuple[Any, int]:
        """解析 RESP 数据，返回 (值, 消耗的字节数)"""
        if not data:
            return None, 0
        
        type_byte = chr(data[0])
        
        if type_byte == '+':  # Simple String
            end = data.find(RESPParser.CRLF)
            if end == -1:
                return None, 0
            return data[1:end].decode('utf-8'), end + 2
        
        elif type_byte == '-':  # Error
            end = data.find(RESPParser.CRLF)
            if end == -1:
                return None, 0
            return RedisError(data[1:end].decode('utf-8')), end + 2
        
        elif type_byte == ':':  # Integer
            end = data.find(RESPParser.CRLF)
            if end == -1:
                return None, 0
            return int(data[1:end]), end + 2
        
        elif type_byte == '$':  # Bulk String
            end = data.find(RESPParser.CRLF)
            if end == -1:
                return None, 0
            length = int(data[1:end])
            if length == -1:
                return None, end + 2
            start = end + 2
            return data[start:start + length].decode('utf-8'), start + length + 2
        
        elif type_byte == '*':  # Array
            end = data.find(RESPParser.CRLF)
            if end == -1:
                return None, 0
            count = int(data[1:end])
            if count == -1:
                return None, end + 2
            
            items = []
            offset = end + 2
            for _ in range(count):
                item, consumed = RESPParser.parse(data[offset:])
                if consumed == 0:
                    return None, 0
                items.append(item)
                offset += consumed
            return items, offset
        
        else:
            # 内联命令 (兼容模式)
            end = data.find(RESPParser.CRLF)
            if end == -1:
                return None, 0
            line = data[:end].decode('utf-8')
            return line.split(), end + 2
    
    @staticmethod
    def encode(value: Any) -> bytes:
        """将值编码为 RESP 格式"""
        if value is None:
            return b'$-1\r\n'
        elif isinstance(value, bool):
            return f':{1 if value else 0}\r\n'.encode()
        elif isinstance(value, int):
            return f':{value}\r\n'.encode()
        elif isinstance(value, RedisError):
            return f'-{str(value)}\r\n'.encode()
        elif isinstance(value, str):
            encoded = value.encode('utf-8')
            return f'${len(encoded)}\r\n'.encode() + encoded + b'\r\n'
        elif isinstance(value, (list, tuple)):
            result = f'*{len(value)}\r\n'.encode()
            for item in value:
                result += RESPParser.encode(item)
            return result
        elif isinstance(value, dict):
            result = f'*{len(value) * 2}\r\n'.encode()
            for k, v in value.items():
                result += RESPParser.encode(k)
                result += RESPParser.encode(v)
            return result
        elif isinstance(value, bytes):
            return f'${len(value)}\r\n'.encode() + value + b'\r\n'
        else:
            return RESPParser.encode(str(value))


class MemoryStore:
    """内存数据存储"""
    
    def __init__(self, max_memory_mb: int = 128):
        self.data: Dict[str, Any] = {}
        self.expires: Dict[str, float] = {}
        self.max_memory = max_memory_mb * 1024 * 1024
        self.current_memory = 0
        self.lock = threading.RLock()
        
        # 数据结构类型
        self.type_map: Dict[str, str] = {}  # key -> type (string, list, hash, set)
    
    def _check_memory(self, size: int = 0) -> bool:
        """检查内存限制"""
        return self.current_memory + size <= self.max_memory
    
    def _evict_if_needed(self):
        """LRU 淘汰策略"""
        while self.current_memory > self.max_memory * 0.9 and self.data:
            # 简单 LRU: 删除最早过期的 key 或随机 key
            if self.expires:
                oldest_key = min(self.expires.keys(), key=lambda k: self.expires[k])
                self._delete_key(oldest_key)
            else:
                # 随机删除一个
                key = next(iter(self.data))
                self._delete_key(key)
    
    def _estimate_size(self, value: Any) -> int:
        """估算值占用内存大小"""
        if value is None:
            return 0
        elif isinstance(value, str):
            return len(value.encode('utf-8')) + 32
        elif isinstance(value, (list, tuple)):
            return sum(self._estimate_size(v) for v in value) + 64
        elif isinstance(value, dict):
            return sum(self._estimate_size(k) + self._estimate_size(v) for k, v in value.items()) + 64
        elif isinstance(value, set):
            return sum(self._estimate_size(v) for v in value) + 64
        else:
            return 32
    
    def _delete_key(self, key: str):
        """删除 key 并更新内存统计"""
        if key in self.data:
            self.current_memory -= self._estimate_size(self.data[key])
            del self.data[key]
            self.type_map.pop(key, None)
        self.expires.pop(key, None)
    
    def is_expired(self, key: str) -> bool:
        """检查 key 是否过期"""
        if key in self.expires:
            if time.time() > self.expires[key]:
                self._delete_key(key)
                return True
        return False
    
    def cleanup_expired(self):
        """清理过期 key"""
        now = time.time()
        expired_keys = [k for k, exp in self.expires.items() if now > exp]
        for key in expired_keys:
            self._delete_key(key)
    
    def get(self, key: str) -> Optional[Any]:
        if self.is_expired(key):
            return None
        return self.data.get(key)
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        with self.lock:
            # 删除旧值
            if key in self.data:
                self._delete_key(key)
            
            # 检查内存
            size = self._estimate_size(value)
            if not self._check_memory(size):
                self._evict_if_needed()
                if not self._check_memory(size):
                    return False
            
            self.data[key] = value
            self.current_memory += size
            self.type_map[key] = 'string'
            
            if ttl:
                self.expires[key] = time.time() + ttl
            
            return True
    
    def delete(self, *keys) -> int:
        with self.lock:
            count = 0
            for key in keys:
                if key in self.data:
                    self._delete_key(key)
                    count += 1
            return count
    
    def exists(self, *keys) -> int:
        count = 0
        for key in keys:
            if not self.is_expired(key) and key in self.data:
                count += 1
        return count
    
    def keys(self, pattern: str = '*') -> List[str]:
        import fnmatch
        result = []
        for key in list(self.data.keys()):
            if not self.is_expired(key):
                if fnmatch.fnmatch(key, pattern):
                    result.append(key)
        return result
    
    def set_ttl(self, key: str, ttl: int) -> bool:
        if self.is_expired(key) or key not in self.data:
            return False
        self.expires[key] = time.time() + ttl
        return True
    
    def get_ttl(self, key: str) -> int:
        if key not in self.data:
            return -2
        if self.is_expired(key):
            return -2
        if key not in self.expires:
            return -1
        return int(self.expires[key] - time.time())
    
    # List 操作
    def lpush(self, key: str, *values) -> int:
        with self.lock:
            if key not in self.data:
                self.data[key] = list(values)
                self.type_map[key] = 'list'
            elif self.type_map.get(key) != 'list':
                raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
            else:
                self.data[key] = list(values) + self.data[key]
            return len(self.data[key])
    
    def rpush(self, key: str, *values) -> int:
        with self.lock:
            if key not in self.data:
                self.data[key] = list(values)
                self.type_map[key] = 'list'
            elif self.type_map.get(key) != 'list':
                raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
            else:
                self.data[key].extend(values)
            return len(self.data[key])
    
    def lpop(self, key: str) -> Optional[Any]:
        with self.lock:
            if key not in self.data or self.type_map.get(key) != 'list':
                return None
            if self.data[key]:
                return self.data[key].pop(0)
            return None
    
    def rpop(self, key: str) -> Optional[Any]:
        with self.lock:
            if key not in self.data or self.type_map.get(key) != 'list':
                return None
            if self.data[key]:
                return self.data[key].pop()
            return None
    
    def llen(self, key: str) -> int:
        if self.is_expired(key) or key not in self.data:
            return 0
        if self.type_map.get(key) != 'list':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return len(self.data.get(key, []))
    
    def lrange(self, key: str, start: int, end: int) -> List[Any]:
        if self.is_expired(key) or key not in self.data:
            return []
        if self.type_map.get(key) != 'list':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        lst = self.data[key]
        if end == -1:
            return lst[start:]
        return lst[start:end + 1]
    
    # Hash 操作
    def hset(self, key: str, field: str, value: Any) -> int:
        with self.lock:
            if key not in self.data:
                self.data[key] = {}
                self.type_map[key] = 'hash'
            elif self.type_map.get(key) != 'hash':
                raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
            
            is_new = field not in self.data[key]
            self.data[key][field] = value
            return 1 if is_new else 0
    
    def hget(self, key: str, field: str) -> Optional[Any]:
        if self.is_expired(key) or key not in self.data:
            return None
        if self.type_map.get(key) != 'hash':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return self.data[key].get(field)
    
    def hgetall(self, key: str) -> Dict[str, Any]:
        if self.is_expired(key) or key not in self.data:
            return {}
        if self.type_map.get(key) != 'hash':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return self.data[key]
    
    def hdel(self, key: str, *fields) -> int:
        with self.lock:
            if key not in self.data:
                return 0
            if self.type_map.get(key) != 'hash':
                raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
            count = 0
            for f in fields:
                if f in self.data[key]:
                    del self.data[key][f]
                    count += 1
            return count
    
    def hexists(self, key: str, field: str) -> bool:
        if self.is_expired(key) or key not in self.data:
            return False
        if self.type_map.get(key) != 'hash':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return field in self.data[key]
    
    def hkeys(self, key: str) -> List[str]:
        if self.is_expired(key) or key not in self.data:
            return []
        if self.type_map.get(key) != 'hash':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return list(self.data[key].keys())
    
    def hvals(self, key: str) -> List[Any]:
        if self.is_expired(key) or key not in self.data:
            return []
        if self.type_map.get(key) != 'hash':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return list(self.data[key].values())
    
    # Set 操作
    def sadd(self, key: str, *members) -> int:
        with self.lock:
            if key not in self.data:
                self.data[key] = set()
                self.type_map[key] = 'set'
            elif self.type_map.get(key) != 'set':
                raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
            
            count = 0
            for m in members:
                if m not in self.data[key]:
                    self.data[key].add(m)
                    count += 1
            return count
    
    def srem(self, key: str, *members) -> int:
        with self.lock:
            if key not in self.data:
                return 0
            if self.type_map.get(key) != 'set':
                raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
            
            count = 0
            for m in members:
                if m in self.data[key]:
                    self.data[key].remove(m)
                    count += 1
            return count
    
    def smembers(self, key: str) -> set:
        if self.is_expired(key) or key not in self.data:
            return set()
        if self.type_map.get(key) != 'set':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return self.data[key]
    
    def sismember(self, key: str, member: str) -> bool:
        if self.is_expired(key) or key not in self.data:
            return False
        if self.type_map.get(key) != 'set':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return member in self.data[key]
    
    def scard(self, key: str) -> int:
        if self.is_expired(key) or key not in self.data:
            return 0
        if self.type_map.get(key) != 'set':
            raise RedisError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return len(self.data[key])
    
    # 计数器操作
    def incr(self, key: str) -> int:
        with self.lock:
            value = self.get(key)
            if value is None:
                value = 0
            elif not isinstance(value, (int, str)) or (isinstance(value, str) and not value.lstrip('-').isdigit()):
                raise RedisError("value is not an integer or out of range")
            else:
                value = int(value)
            
            new_value = value + 1
            self.set(key, str(new_value))
            return new_value
    
    def decr(self, key: str) -> int:
        with self.lock:
            value = self.get(key)
            if value is None:
                value = 0
            elif not isinstance(value, (int, str)) or (isinstance(value, str) and not value.lstrip('-').isdigit()):
                raise RedisError("value is not an integer or out of range")
            else:
                value = int(value)
            
            new_value = value - 1
            self.set(key, str(new_value))
            return new_value
    
    def incrby(self, key: str, increment: int) -> int:
        with self.lock:
            value = self.get(key)
            if value is None:
                value = 0
            elif not isinstance(value, (int, str)) or (isinstance(value, str) and not value.lstrip('-').isdigit()):
                raise RedisError("value is not an integer or out of range")
            else:
                value = int(value)
            
            new_value = value + increment
            self.set(key, str(new_value))
            return new_value
    
    def flushdb(self):
        """清空当前数据库"""
        with self.lock:
            self.data.clear()
            self.expires.clear()
            self.type_map.clear()
            self.current_memory = 0
    
    def save(self, filepath: str):
        """保存数据到文件"""
        with self.lock:
            data = {
                'data': self.data,
                'expires': self.expires,
                'type_map': self.type_map
            }
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, default=str)
    
    def load(self, filepath: str):
        """从文件加载数据"""
        if os.path.exists(filepath):
            with self.lock:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.data = data.get('data', {})
                    self.expires = data.get('expires', {})
                    self.type_map = data.get('type_map', {})
                    
                    # 转换 expires 中的字符串时间为浮点数
                    self.expires = {k: float(v) for k, v in self.expires.items()}


class RedisServer:
    """Redis 服务端"""
    
    def __init__(self, host: str = '0.0.0.0', port: int = 6379, 
                 max_memory_mb: int = 128, password: Optional[str] = None):
        self.host = host
        self.port = port
        self.password = password
        self.store = MemoryStore(max_memory_mb)
        self.running = False
        self.server_socket = None
        self.clients = []
        self.start_time = time.time()
        self.data_dir = os.path.join(os.path.dirname(__file__), '..', 'data', 'redis')
        self.rdb_file = os.path.join(self.data_dir, 'dump.rdb')
        
        # 统计
        self.stats = {
            'total_connections': 0,
            'total_commands': 0,
            'total_bytes_read': 0,
            'total_bytes_written': 0
        }
        
        # 加载持久化数据
        self._load_data()
    
    def _load_data(self):
        """加载持久化数据"""
        try:
            if os.path.exists(self.rdb_file):
                self.store.load(self.rdb_file)
                print(f"[INFO] 加载持久化数据成功: {self.rdb_file}")
        except Exception as e:
            print(f"[WARN] 加载持久化数据失败: {e}")
    
    def _save_data(self):
        """保存持久化数据"""
        try:
            os.makedirs(os.path.dirname(self.rdb_file), exist_ok=True)
            self.store.save(self.rdb_file)
            print(f"[INFO] 保存持久化数据成功: {self.rdb_file}")
        except Exception as e:
            print(f"[ERROR] 保存持久化数据失败: {e}")
    
    def handle_command(self, command: List[str]) -> Any:
        """处理 Redis 命令"""
        if not command:
            return RedisError("empty command")
        
        cmd = command[0].upper()
        args = command[1:]
        self.stats['total_commands'] += 1
        
        # 认证检查
        if self.password and cmd != 'AUTH':
            return RedisError("NOAUTH Authentication required")
        
        # 基础命令
        if cmd == 'PING':
            if len(args) == 0:
                return 'PONG'
            return args[0]
        
        elif cmd == 'ECHO':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'echo' command")
            return args[0]
        
        elif cmd == 'AUTH':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'auth' command")
            if args[0] == self.password:
                return 'OK'
            return RedisError("ERR invalid password")
        
        # 键值操作
        elif cmd == 'SET':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'set' command")
            key, value = args[0], args[1]
            ttl = None
            if len(args) >= 4 and args[2].upper() == 'EX':
                ttl = int(args[3])
            elif len(args) >= 4 and args[2].upper() == 'PX':
                ttl = int(args[3]) // 1000
            self.store.set(key, value, ttl)
            return 'OK'
        
        elif cmd == 'GET':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'get' command")
            return self.store.get(args[0])
        
        elif cmd == 'DEL':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'del' command")
            return self.store.delete(*args)
        
        elif cmd == 'EXISTS':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'exists' command")
            return self.store.exists(*args)
        
        elif cmd == 'KEYS':
            pattern = args[0] if args else '*'
            return self.store.keys(pattern)
        
        elif cmd == 'TTL':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'ttl' command")
            return self.store.get_ttl(args[0])
        
        elif cmd == 'EXPIRE':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'expire' command")
            return 1 if self.store.set_ttl(args[0], int(args[1])) else 0
        
        elif cmd == 'TYPE':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'type' command")
            key = args[0]
            if self.store.is_expired(key) or key not in self.store.data:
                return 'none'
            return self.store.type_map.get(key, 'string')
        
        # 计数器
        elif cmd == 'INCR':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'incr' command")
            return self.store.incr(args[0])
        
        elif cmd == 'DECR':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'decr' command")
            return self.store.decr(args[0])
        
        elif cmd == 'INCRBY':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'incrby' command")
            return self.store.incrby(args[0], int(args[1]))
        
        # List 操作
        elif cmd == 'LPUSH':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'lpush' command")
            return self.store.lpush(args[0], *args[1:])
        
        elif cmd == 'RPUSH':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'rpush' command")
            return self.store.rpush(args[0], *args[1:])
        
        elif cmd == 'LPOP':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'lpop' command")
            return self.store.lpop(args[0])
        
        elif cmd == 'RPOP':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'rpop' command")
            return self.store.rpop(args[0])
        
        elif cmd == 'LLEN':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'llen' command")
            return self.store.llen(args[0])
        
        elif cmd == 'LRANGE':
            if len(args) < 3:
                return RedisError("wrong number of arguments for 'lrange' command")
            return self.store.lrange(args[0], int(args[1]), int(args[2]))
        
        # Hash 操作
        elif cmd == 'HSET':
            if len(args) < 3:
                return RedisError("wrong number of arguments for 'hset' command")
            return self.store.hset(args[0], args[1], args[2])
        
        elif cmd == 'HGET':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'hget' command")
            return self.store.hget(args[0], args[1])
        
        elif cmd == 'HGETALL':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'hgetall' command")
            return self.store.hgetall(args[0])
        
        elif cmd == 'HDEL':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'hdel' command")
            return self.store.hdel(args[0], *args[1:])
        
        elif cmd == 'HEXISTS':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'hexists' command")
            return 1 if self.store.hexists(args[0], args[1]) else 0
        
        elif cmd == 'HKEYS':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'hkeys' command")
            return self.store.hkeys(args[0])
        
        elif cmd == 'HVALS':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'hvals' command")
            return self.store.hvals(args[0])
        
        # Set 操作
        elif cmd == 'SADD':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'sadd' command")
            return self.store.sadd(args[0], *args[1:])
        
        elif cmd == 'SREM':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'srem' command")
            return self.store.srem(args[0], *args[1:])
        
        elif cmd == 'SMEMBERS':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'smembers' command")
            return list(self.store.smembers(args[0]))
        
        elif cmd == 'SISMEMBER':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'sismember' command")
            return 1 if self.store.sismember(args[0], args[1]) else 0
        
        elif cmd == 'SCARD':
            if len(args) < 1:
                return RedisError("wrong number of arguments for 'scard' command")
            return self.store.scard(args[0])
        
        # 服务器管理
        elif cmd == 'INFO':
            section = args[0] if args else 'server'
            return self._get_info(section)
        
        elif cmd == 'DBSIZE':
            return len(self.store.data)
        
        elif cmd == 'FLUSHDB':
            self.store.flushdb()
            return 'OK'
        
        elif cmd == 'FLUSHALL':
            self.store.flushdb()
            return 'OK'
        
        elif cmd == 'SAVE':
            self._save_data()
            return 'OK'
        
        elif cmd == 'COMMAND':
            # 返回命令文档 (简化版)
            return ['SET', 'GET', 'DEL', 'KEYS', 'LPUSH', 'RPUSH', 'HSET', 'HGET', 'SADD']
        
        elif cmd == 'CONFIG':
            if len(args) < 2:
                return RedisError("wrong number of arguments for 'config' command")
            if args[0].upper() == 'GET':
                return []  # 简化实现
            return 'OK'
        
        elif cmd == 'QUIT':
            return 'OK'
        
        elif cmd == 'SHUTDOWN':
            self.running = False
            self._save_data()
            return 'OK'
        
        else:
            return RedisError(f"unknown command '{cmd}'")
    
    def _get_info(self, section: str) -> str:
        """获取服务器信息"""
        info_sections = {
            'server': f"""# Server
redis_version:7.0.0-py
redis_mode:standalone
os:{sys.platform}
arch_bits:64
tcp_port:{self.port}
uptime_in_seconds:{int(time.time() - self.start_time)}
uptime_in_days:{int((time.time() - self.start_time) / 86400)}
""",
            'memory': f"""# Memory
used_memory:{self.store.current_memory}
used_memory_human:{self.store.current_memory // 1024}K
maxmemory:{self.store.max_memory}
maxmemory_human:{self.store.max_memory // 1024 // 1024}M
""",
            'stats': f"""# Stats
total_connections_received:{self.stats['total_connections']}
total_commands_processed:{self.stats['total_commands']}
total_net_input_bytes:{self.stats['total_bytes_read']}
total_net_output_bytes:{self.stats['total_bytes_written']}
""",
            'keyspace': f"""# Keyspace
db0:keys={len(self.store.data)},expires={len(self.store.expires)}
"""
        }
        
        if section == 'default':
            return ''.join(info_sections.values())
        return info_sections.get(section, info_sections['server'])
    
    def handle_client(self, client_socket: socket.socket, address: tuple):
        """处理客户端连接"""
        self.stats['total_connections'] += 1
        print(f"[INFO] 客户端连接: {address}")
        
        buffer = b''
        authenticated = self.password is None
        
        try:
            while self.running:
                try:
                    data = client_socket.recv(4096)
                    if not data:
                        break
                    
                    self.stats['total_bytes_read'] += len(data)
                    buffer += data
                    
                    # 尝试解析完整的命令
                    while buffer:
                        command, consumed = RESPParser.parse(buffer)
                        if consumed == 0:
                            break
                        
                        buffer = buffer[consumed:]
                        
                        # 处理命令
                        if isinstance(command, list):
                            result = self.handle_command(command)
                        elif isinstance(command, str):
                            # 内联命令
                            parts = command.split()
                            result = self.handle_command(parts)
                        else:
                            result = RedisError("invalid command format")
                        
                        # 发送响应
                        response = RESPParser.encode(result)
                        client_socket.sendall(response)
                        self.stats['total_bytes_written'] += len(response)
                
                except socket.timeout:
                    continue
                except ConnectionResetError:
                    break
        
        except Exception as e:
            print(f"[ERROR] 处理客户端 {address} 时出错: {e}")
        
        finally:
            client_socket.close()
            print(f"[INFO] 客户端断开: {address}")
    
    def start(self):
        """启动服务"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(100)
        self.server_socket.settimeout(1)
        self.running = True
        
        print(f"[INFO] Redis 服务启动于 {self.host}:{self.port}")
        print(f"[INFO] 最大内存: {self.store.max_memory // 1024 // 1024}MB")
        print(f"[INFO] 密码保护: {'是' if self.password else '否'}")
        print(f"[INFO] 持久化文件: {self.rdb_file}")
        print("-" * 50)
        
        # 启动定期清理过期 key 的线程
        cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True)
        cleanup_thread.start()
        
        # 启动定期保存的线程
        save_thread = threading.Thread(target=self._save_worker, daemon=True)
        save_thread.start()
        
        try:
            while self.running:
                try:
                    client_socket, address = self.server_socket.accept()
                    client_socket.settimeout(300)  # 5分钟超时
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, address),
                        daemon=True
                    )
                    client_thread.start()
                except socket.timeout:
                    continue
        
        except KeyboardInterrupt:
            print("\n[INFO] 收到中断信号，正在关闭...")
        
        finally:
            self.stop()
    
    def stop(self):
        """停止服务"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
        self._save_data()
        print("[INFO] Redis 服务已停止")
    
    def _cleanup_worker(self):
        """定期清理过期 key"""
        while self.running:
            time.sleep(60)
            self.store.cleanup_expired()
    
    def _save_worker(self):
        """定期保存数据"""
        while self.running:
            time.sleep(300)  # 每5分钟保存一次
            if self.running:
                self._save_data()


def main():
    parser = argparse.ArgumentParser(description='Python 实现的轻量级 Redis 服务端')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址 (默认: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=6379, help='监听端口 (默认: 6379)')
    parser.add_argument('--max-memory', type=int, default=128, help='最大内存 MB (默认: 128)')
    parser.add_argument('--password', default=None, help='认证密码 (可选)')
    parser.add_argument('--daemon', action='store_true', help='以守护进程运行')
    
    args = parser.parse_args()
    
    server = RedisServer(
        host=args.host,
        port=args.port,
        max_memory_mb=args.max_memory,
        password=args.password
    )
    
    # 信号处理
    def signal_handler(sig, frame):
        server.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    server.start()


if __name__ == '__main__':
    main()
