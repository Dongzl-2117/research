-- 外链图片：Mac mini 上的代理开着 fake-ip，容器里 i0.wp.com 和 stratechery.com 都解析成 198.18.x.x，
-- 只连公网的检查不放行。11-09 夜间 300 个新地址一张没下下来，030 改的「先下原站」也一样被拦。
-- research-fetch-images 现在对 Stratechery 原站和 WordPress 图片 CDN 放行代理的假地址。
-- 被这条检查拦过的失败记录按 1、3、7、30 天的间隔要等很久才重试，删掉就从下一次下载起重新排队。
-- 只删失败的、错误里写着「解析到内网或本机」的（原站和 CDN 哪边被拦都算），只删这几个图床的；
-- 成功的记录、别的原因的失败（比如两边都 404）和别的图床不动。再跑一遍结果一样。
DELETE FROM image_fetches
WHERE NOT ok
  AND url ~* '^https?://(i[0-3]\.wp\.com|(www\.)?stratechery\.com)/'
  AND error ~ '地址解析到内网或本机';
