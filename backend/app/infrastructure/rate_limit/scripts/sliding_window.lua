-- 原子滑动窗口限流（T028）。
-- 计数、清理与判断在单次脚本内完成；窗口成员使用微秒时间戳保证唯一性。
-- 返回值：{allowed, retry_after}；allowed=1 表示放行，allowed=0 表示拒绝并给出
-- 至少等待秒数（不小于 1）。脚本不暴露内部键或成员内容。
--
-- KEYS[1] = 限流键（rl:<policy>:<window>:<subject>）
-- ARGV[1] = now 毫秒
-- ARGV[2] = 窗口毫秒
-- ARGV[3] = 阈值（窗口内最大事件数）

local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)

local count = redis.call('ZCARD', key)
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = 1
    if oldest[2] then
        local remaining = tonumber(oldest[2]) + window_ms - now_ms
        if remaining > 0 then
            retry_after = math.ceil(remaining / 1000)
        end
    end
    if retry_after < 1 then retry_after = 1 end
    redis.call('PEXPIRE', key, window_ms)
    return {0, retry_after}
end

-- 微秒唯一成员：秒与微秒来自服务器时钟，避免同一毫秒内事件被去重。
local time_parts = redis.call('TIME')
local member = (tonumber(time_parts[1]) * 1000000) + tonumber(time_parts[2])
redis.call('ZADD', key, now_ms, tostring(member))
-- TTL 为窗口 + 清理余量，避免计数残留长期占用。
redis.call('PEXPIRE', key, window_ms + 1000)
return {1, 0}
