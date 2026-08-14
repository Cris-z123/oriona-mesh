-- 本地开发容器首次初始化：创建自动化测试数据库。
-- 测试夹具会对该库 drop_all + create_all 并 TRUNCATE 全部表，务必使用独立库。
CREATE DATABASE orionamesh_test OWNER orionamesh;
