-- about=true 却没有判断的脏行改成 about=false（缓存「没下判断」），不进时间线。
UPDATE company_views
SET about = false
WHERE about
  AND (judgment IS NULL OR btrim(judgment) = '' OR judgment !~ '[[:alnum:]一-鿿]');
