"""
Async WX Article Fetcher - 完全异步版本
"""
import random
import time
import base64
import re
import os
import asyncio
from datetime import datetime
from typing import Dict
from bs4 import BeautifulSoup

from .playwright_driver import PlaywrightController
from core.print import print_error, print_info, print_success, print_warning
from core.config import cfg


class WXArticleFetcher:
    """
    异步微信公众号文章获取器
    
    完全基于 async/await,与 FastAPI 完美兼容
    """
    
    def __init__(self, wait_timeout: int = 10000):
        """初始化文章获取器"""
        self.wait_timeout = wait_timeout
        self.controller = PlaywrightController()
        self.browser_proxy_url = ""
        
        if cfg.get("proxy.enabled", False):
            self.browser_proxy_url = cfg.get("proxy.http_url", "")
            
    async def get_article_content(self, url: str) -> Dict:
        """
        获取文章内容(异步)

        Args:
            url: 文章URL

        Returns:
            文章信息字典，包含:
            - id: 文章ID
            - title: 标题
            - author: 作者
            - description: 描述
            - topic_image: 题图
            - publish_time: 发布时间戳
            - content: 正文HTML
            - mp_info: 公众号信息 {mp_name, logo, biz}
            - mp_id: 公众号ID
            - fetch_error: 错误信息
        """
        info = {
            "id": self.extract_id_from_url(url),
            "title": "",
            "author": "",
            "description": "",
            "topic_image": "",
            "publish_time": 0,
            "content": "",
            "mp_info": {
                "mp_name": "",
                "logo": "",
                "biz": ""
            },
            "mp_id": "",
            "fetch_error": ""
        }

        try:
            # 使用异步上下文管理器
            async with PlaywrightController(
                proxy_url=self.browser_proxy_url,
                mobile_mode=True
            ) as controller:

                # 打开URL
                success = await controller.open_url(url, timeout=self.wait_timeout)
                if not success:
                    raise Exception("页面加载失败")

                page = controller.page

                # 等待页面加载
                await asyncio.sleep(2)

                # 获取页面内容
                body = await page.content()
                body_text = await page.locator("body").text_content()

                # 检查各种异常情况
                if "当前环境异常，完成验证后即可继续访问" in body_text:
                    info["content"] = ""
                    info["fetch_error"] = "当前环境异常，完成验证后即可继续访问"
                    return info

                if "该内容已被发布者删除" in body_text or "The content has been deleted by the author." in body_text:
                    info["content"] = "DELETED"
                    info["fetch_error"] = "该内容已被发布者删除"
                    return info

                if "内容审核中" in body_text:
                    info["content"] = "DELETED"
                    info["fetch_error"] = "内容审核中"
                    return info

                if "该内容暂时无法查看" in body_text:
                    info["content"] = "DELETED"
                    info["fetch_error"] = "该内容暂时无法查看"
                    return info

                if "违规无法查看" in body_text or "Unable to view this content because it violates regulation" in body_text:
                    info["content"] = "DELETED"
                    info["fetch_error"] = "违规无法查看"
                    return info

                if "发送失败无法查看" in body_text:
                    info["content"] = "DELETED"
                    info["fetch_error"] = "发送失败无法查看"
                    return info

                # 获取文章基本信息
                title = await page.locator('meta[property="og:title"]').get_attribute("content")
                author = await page.locator('meta[property="og:article:author"]').get_attribute("content")
                description = await page.locator('meta[property="og:description"]').get_attribute("content")
                topic_image = await page.locator('meta[property="twitter:image"]').get_attribute("content")

                if not title:
                    title = await page.evaluate('() => document.title')

                # 获取发布时间
                publish_time = await self._extract_publish_time(page)

                # 获取内容
                content = await page.locator('#js_content').inner_html()
                if not content:
                    content = await page.locator('#js_article').inner_html()

                # 更新基本信息
                info["title"] = title or ""
                info["author"] = author or ""
                info["description"] = description or ""
                info["topic_image"] = topic_image or ""
                info["publish_time"] = publish_time
                info["content"] = content or ""

                # 获取公众号信息
                try:
                    # 获取公众号头像
                    logo_src = None
                    selectors = [
                        '#js_like_profile_bar .wx_follow_avatar img',
                        '#js_like_profile_bar img.wx_follow_avatar_pic',
                        '.wx_follow_avatar img'
                    ]

                    for selector in selectors:
                        try:
                            ele_logo = page.locator(selector)
                            logo_src = await ele_logo.get_attribute('src', timeout=5000)
                            if logo_src:
                                print_success(f"使用选择器 {selector} 成功获取公众号头像")
                                break
                        except Exception:
                            continue

                    if not logo_src:
                        try:
                            logo_src = await page.locator('meta[property="og:image"]').get_attribute("content", timeout=3000)
                        except Exception:
                            pass

                    # 获取公众号名称
                    mp_name = None
                    try:
                        mp_name = await page.evaluate('() => { const el = document.getElementById("js_wx_follow_nickname"); return el ? el.textContent : null; }')
                    except Exception:
                        pass

                    if not mp_name:
                        try:
                            mp_name = await page.locator('meta[property="og:article:author"]').get_attribute("content", timeout=3000)
                        except Exception:
                            pass

                    # 获取biz
                    biz = None
                    try:
                        biz = await page.evaluate('() => window.biz')
                    except Exception:
                        pass

                    if not biz:
                        biz = self._extract_biz(url, content or "")

                    info["mp_info"] = {
                        "mp_name": mp_name or "未知公众号",
                        "logo": logo_src or "",
                        "biz": biz or ""
                    }

                    # 生成 mp_id
                    if biz:
                        try:
                            info["mp_id"] = "MP_WXS_" + base64.b64decode(biz).decode("utf-8")
                        except Exception:
                            info["mp_id"] = ""

                except Exception as e:
                    print_error(f"获取公众号信息失败: {str(e)}")
                    info["mp_info"] = {
                        "mp_name": "未知公众号",
                        "logo": "",
                        "biz": ""
                    }
                    info["mp_id"] = ""

                return info

        except Exception as e:
            info["fetch_error"] = str(e)
            print_error(f"获取文章内容失败: {str(e)}")
            return info
            
    async def _extract_publish_time(self, page) -> int:
        """
        提取发布时间(异步)
        """
        try:
            # 尝试从 meta 标签获取
            publish_time_str = await page.locator('#publish_time').text_content()
            
            if publish_time_str:
                return self._convert_publish_time_to_timestamp(publish_time_str)
                
            # 尝试从页面内容获取
            content = await page.content()
            
            # 常见的时间格式
            patterns = [
                r'publish_time\s*=\s*["\']([^"\']+)["\']',
                r'var\s+publish_time\s*=\s*["\']([^"\']+)["\']',
                r'create_time\s*=\s*["\']([^"\']+)["\']',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, content)
                if match:
                    return self._convert_publish_time_to_timestamp(match.group(1))
                    
            # 返回当前时间
            return int(datetime.now().timestamp())
            
        except Exception as e:
            print_warning(f"提取发布时间失败: {str(e)}")
            return int(datetime.now().timestamp())
            
    def _convert_publish_time_to_timestamp(self, publish_time_str: str) -> int:
        """
        将发布时间字符串转换为时间戳
        支持 "2026年4月9日 22:44" 等中文日期格式（月份/日期可以是单位数）
        """
        try:
            # 预处理：补零对齐中文日期中的单位数月份和日期
            # 例如 "2026年4月9日 22:44" -> "2026年04月09日 22:44"
            normalized_str = re.sub(
                r'(\d{4})年(\d{1,2})月(\d{1,2})日',
                lambda m: f"{m.group(1)}年{m.group(2).zfill(2)}月{m.group(3).zfill(2)}日",
                publish_time_str
            )
            
            formats = [
                "%Y-%m-%d %H:%M:%S",
                "%Y年%m月%d日 %H:%M",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d",
                "%Y年%m月%d日",
                "%m月%d日",
            ]
            
            for fmt in formats:
                try:
                    if fmt == "%m月%d日":
                        current_date = datetime.now()
                        current_year = current_date.year
                        full_time_str = f"{current_year}年{normalized_str}"
                        dt = datetime.strptime(full_time_str, "%Y年%m月%d日")
                        
                        if dt > current_date:
                            dt = dt.replace(year=current_year - 1)
                    else:
                        dt = datetime.strptime(normalized_str, fmt)
                    return int(dt.timestamp())
                except ValueError:
                    continue
                    
            return int(datetime.now().timestamp())
            
        except Exception as e:
            print_error(f"时间转换失败: {e}")
            return int(datetime.now().timestamp())
            
    def _extract_biz(self, url: str, content: str) -> str:
        """
        提取 biz 参数
        """
        # 从 URL 提取
        match = re.search(r'[?&]__biz=([^&]+)', url)
        if match:
            return match.group(1)
            
        # 从内容提取
        match = re.search(r'var\s+biz\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            return match.group(1)
            
        return ""

    def extract_id_from_url(self, url: str) -> str:
        """从微信文章URL中提取ID
        
        Args:
            url: 文章URL
            
        Returns:
            文章ID字符串，如果提取失败返回空字符串
        """
        try:
            # 从URL中提取ID部分
            match = re.search(r'/s/([A-Za-z0-9_-]+)', url)
            if not match:
                return ""
                
            id_str = match.group(1)
            
            # 添加必要的填充
            padding = 4 - len(id_str) % 4
            if padding != 4:
                id_str += '=' * padding
                
            # 尝试解码base64
            try:
                id_number = base64.b64decode(id_str).decode("utf-8")
                return id_number
            except Exception:
                # 如果base64解码失败，返回原始ID字符串
                return match.group(1)
                
        except Exception as e:
            print_error(f"提取文章ID失败: {e}")
            return ""

    @staticmethod
    def get_description(content: str, length: int = 200) -> str:
        """
        从文章内容中提取描述文本
        
        Args:
            content: HTML 内容
            length: 描述文本的最大长度
            
        Returns:
            描述文本
        """
        if not content:
            return ""
        try:
            soup = BeautifulSoup(content, 'html.parser')
            js_content_div = soup
            if js_content_div is None:
                return ""
            text = js_content_div.get_text().strip().strip("\n").replace("\n", " ").replace("\r", " ")
            return text[:length] + "..." if len(text) > length else text
        except Exception:
            return ""


# Web 工具类(兼容旧代码)
class Web:
    """Web 工具类,提供文章内容清理等功能"""
    @staticmethod
    def fix_images(content:str)->str:
            try:
                soup = BeautifulSoup(content, 'html.parser')
                # 找到内容
                js_content_div = soup
                # 移除style属性中的visibility: hidden;
                if js_content_div is None:
                    return ""
                js_content_div.attrs.pop('style', None)
                # 找到所有的img标签
                img_tags = js_content_div.find_all('img')
                # 遍历每个img标签并修改属性，设置宽度为1080p
                for img_tag in img_tags:
                    if 'data-src' in img_tag.attrs:
                        img_tag['src'] = img_tag['data-src']
                        del img_tag['data-src']
                    if 'style' in img_tag.attrs:
                        style = img_tag['style']
                        # 使用正则表达式替换width属性
                        style = re.sub(r'width\s*:\s*\d+\s*px', 'width: 1080px', style)
                        # img_tag['style'] = style
                return  js_content_div.prettify()
            except Exception as e:
                print_error(f"修复图片失败: {str(e)}")
            return content
    @staticmethod
    def clean_article_content(html_content: str,mp_id:str=""):
        from tools.htmltools import htmltools
        html_content=Web.fix_images(html_content)
        # 应用过滤规则
        try:
            from apis.filter_rule import apply_filter_rules
            print(f"[DB] 准备应用过滤规则: mp_id={mp_id}, content_html存在={html_content is not None}")
            if html_content:
                html_content = apply_filter_rules(html_content, mp_id)

        except Exception as e:
            print_warning(f"应用过滤规则失败: {e}")
        if not cfg.get("gather.clean_html",False):
            return html_content
        
        return htmltools.clean_html(str(html_content).strip(),
                                remove_ids=
                                ['content_bottom_interaction',
                                 'activity-name',
                                 'meta_content',
                                 "js_article_bottom_bar",
                                 "js_pc_weapp_code",
                                 "js_novel_card",
                                 "js_pc_qr_code"
                                 ],
                                 remove_selectors=[
                                     "link",
                                     "head",
                                     "script"
                                 ],
                                 remove_attributes=[
                                     {"name":"style","value":"display: none;"},
                                     {"name":"style","value":"display:none;"},
                                     {"name":"aria-hidden","value":"true"},
                                 ],
                                 remove_normal_tag=True
                                 )

    @staticmethod
    def get_article_content(url: str) -> Dict:
        """
        获取文章内容(同步包装器,兼容旧代码)

        注意: 这是一个同步包装器,会阻塞调用线程
        建议使用 WXArticleFetcher().get_article_content() 的 async 版本

        Args:
            url: 文章URL

        Returns:
            文章信息字典
        """
        import asyncio

        try:
            # 创建新的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                # 创建 fetcher 实例
                fetcher = WXArticleFetcher()

                # 运行异步方法
                result = loop.run_until_complete(fetcher.get_article_content(url))

                return result
            finally:
                # 清理事件循环
                loop.close()

        except Exception as e:
            print_error(f"获取文章内容失败: {e}")
            return {}

    @staticmethod
    def get_description(content:str,length:int=200)->str:
        # 防御性检查：确保 content 不是 None
        if not content:
            return ""
        try:
            soup = BeautifulSoup(content, 'html.parser')
            # 找到内容
            js_content_div = soup
            if js_content_div is None:
                return ""
            text = js_content_div.get_text().strip().strip("\n").replace("\n"," ").replace("\r"," ")
            return text[:length]+"..." if len(text)>length else text
        except Exception:
            return ""

    @staticmethod
    def get_image_url(content: str) -> str:
        """
        从内容中提取图片URL

        Args:
            content: HTML 内容或直接的图片URL

        Returns:
            图片URL
        """
        if not content:
            return ""

        # 如果传入的已经是URL，直接返回
        if content.startswith(('http://', 'https://')):
            return content

        try:
            # 使用 BeautifulSoup 解析
            soup = BeautifulSoup(content, 'html.parser')

            # 查找第一个图片标签
            img = soup.find('img')

            if img:
                # 获取 src 属性
                src = img.get('src') or img.get('data-src')
                if src:
                    return src

            return ""

        except Exception as e:
            print_error(f"提取图片URL失败: {e}")
            return content

    @staticmethod
    def proxy_images(content: str) -> str:
        """
        处理文章内容中的图片链接，将微信图片转换为代理URL
        
        微信公众号文章图片特点：
        1. 使用 data-src 属性（懒加载）
        2. URL 来自 mmbiz.qpic.cn 等微信 CDN
        3. 可能需要正确的 referer 才能访问
        
        Args:
            content: HTML 内容
            
        Returns:
            处理后的 HTML 内容
        """
        if not content:
            return content
            
        try:
            soup = BeautifulSoup(content, 'html.parser')
            
            # 处理所有图片标签
            for img in soup.find_all('img'):
                # 获取图片URL（优先 data-src，其次 src）
                img_url = img.get('data-src') or img.get('src')
                
                if img_url and img_url.startswith(('http://', 'https://')):
                    # 将图片URL转换为代理URL
                    from urllib.parse import quote
                    encoded_url = quote(img_url, safe='')
                    proxy_url = f"/static/res/logo/{encoded_url}"
                    
                    # 设置 src 属性（确保图片能显示）
                    img['src'] = proxy_url
                    
                    # 如果有 data-src，也更新它
                    if img.has_attr('data-src'):
                        img['data-src'] = proxy_url
                    
                    # 移除可能阻止图片加载的属性
                    for attr in ['data-type', 'data-ratio', 'data-w']:
                        if img.has_attr(attr):
                            del img[attr]
            
            return str(soup)
            
        except Exception as e:
            print_error(f"处理图片代理失败: {e}")
            return content


# 导入 asyncio(用于文件顶部)
import asyncio
