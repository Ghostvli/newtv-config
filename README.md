# newtv-config

NewTV 电视端接口配置仓库，由 GitHub Actions 定时从在线接口自动解密更新。

## 电视端使用

配置地址（二选一，优先 CDN）：

```
https://cdn.jsdelivr.net/gh/Ghostvli/newtv-config@main/NewTV.json
https://raw.githubusercontent.com/Ghostvli/newtv-config/main/NewTV.json
```

## 自动更新流程

每天北京时间 9:00 / 21:00（GitHub Actions 定时）自动执行：

1. 下载在线接口 `http://www.饭太硬.cc/tv`（伪装成图片的文件；中文域名脚本会自动转 punycode）
2. 解密：取 JPEG 结束标记 `FF D9` 之后的数据 → 去掉 `标记**` 前缀 → Base64 解码 → 配置 JSON
3. 合并 `zte.json`：剔除 `remove_site_keys` 里的站点（移动），前置 ZTE 站点（和园）与直播源（山东联通）
4. 与现有 `NewTV.json` 语义对比，有实质变化才提交推送，并刷新 jsDelivr 缓存

## 手动更新

```bash
# 检查上游是否有更新（不写文件）
python3 update_config.py --url "http://www.饭太硬.cc/tv" --output NewTV.json --zte zte.json --check-only

# 写入并提交
python3 update_config.py --url "http://www.饭太硬.cc/tv" --output NewTV.json --zte zte.json
git add NewTV.json && git commit -m "manual: 更新配置" && git push
```

## 调整 ZTE 保留条目

编辑 `zte.json`（数据驱动，无需改脚本）：

- `sites`：前置的 ZTE 站点列表
- `lives`：前置的 ZTE 直播源列表
- `remove_site_keys`：从上游配置中剔除的站点 key / api

改完提交即可，下次定时任务自动生效；想立即生效可在 GitHub 仓库 Actions 页面手动触发 "Update NewTV config"。
