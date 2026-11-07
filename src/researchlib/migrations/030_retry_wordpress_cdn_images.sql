-- Stratechery 的图挂在 WordPress 图片 CDN（i0.wp.com/stratechery.com/…）上，一张都没下载下来：
-- Mac mini 上 i0.wp.com 解析到代理的 198.18.x.x，只连公网的检查不放行（11-07 约 900 个地址），
-- 另有约 635 个地址 CDN 回了 404。research-fetch-images 改成先下原站的图。
-- 这些失败记录按 1、3、7、30 天的间隔要等很久才重试，删掉就从下一晚起重新排队。
-- 只删失败的、只删 i0 到 i3.wp.com 的；成功的记录和别的图床不动。
DELETE FROM image_fetches WHERE NOT ok AND url ~* '^https?://i[0-3]\.wp\.com/';
