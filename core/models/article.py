from pydantic import Json
from sqlalchemy import BigInteger

from  .base import Base,Column,String,Integer,DateTime,Text,DATA_STATUS
class ArticleBase(Base):
    from_attributes = True
    __tablename__ = 'articles'
    id = Column(String(255), primary_key=True)
    mp_id = Column(String(255),index=True)
    title = Column(String(1000))
    pic_url = Column(String(500))
    url=Column(String(500))
    description=Column(Text)
    extinfo = Column(Text)
    status = Column(Integer,default=1,index=True)
    publish_time = Column(Integer,index=True)
    create_time = Column(Integer,index=True)
    publish_type = Column(Integer,index=True)
    publish_src = Column(Integer,index=True)
    publish_status = Column(Text,index=True)
    original_check_type = Column(Integer,index=True)
    in_profile = Column(Integer,index=True)
    pre_publish_status = Column(Integer,index=True)
    service_type = Column(Integer,index=True)
    item_show_types = Column(Integer,index=True)
    copyright_stat = Column(Integer,index=True)
    has_red_packet_cover = Column(Integer,index=True)
    created_at = Column(DateTime)
    updated_at = Column(BigInteger)
    updated_at_millis = Column(BigInteger,index=True)
    is_export = Column(Integer)
    is_read = Column(Integer, default=0)
    is_favorite = Column(Integer, default=0)
class Article(ArticleBase):
    content = Column(Text)
    content_html = Column(Text)
    
    def to_dict(self):
        """将Article对象转换为字典"""
        return {
            'id': self.id,
            'mp_id': self.mp_id,
            'title': self.title,
            'pic_url': self.pic_url,
            'url': self.url,
            'description': self.description,
            'content': self.content,
            'content_html': self.content_html,
            'status': self.status,
            'publish_time': self.publish_time,
            'created_at': self.created_at.isoformat() if self.created_at and hasattr(self.created_at, "isoformat") else self.created_at,
            'updated_at': self.updated_at.isoformat() if self.updated_at and hasattr(self.updated_at, "isoformat") else self.updated_at,
            'is_export': self.is_export,
            'is_read': self.is_read,
            'is_favorite': self.is_favorite
        }
