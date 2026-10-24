# astrbot_plugin_oil_price · 今日油价

AstrBot 插件：全国油价查询、卡片出图、定时推送与走势统计（数据源：万维 ShowAPI）。

> **私有项目（Private / Internal）**：本仓库未设置开源许可证，未经授权请勿分发。
> 油价数据来自第三方接口，仅供参考，请以当地加油站实际价格为准。

## 功能

- **7 条指令**：`/油价`（文字版）、`/今日油价`（出图版）、`/看看油价`（单油种大字卡）、
  `/我要加油`（加油小票）、`/油价设置`、`/调价窗口`、`/油价推送测试`
- **AI 生图海报**：每日图片由当天文字消息直接交给 bot 已配置模型平台的生图端点生成（图文同源），
  上涨/下调/持平三种场景自动匹配标题与建议文案，5 种风格随机轮换、不连续重复
- **每日定时推送**：推送频率可配（每日 / 每周 / 有变动时）；单省 = 文字版 + 图片版两条消息，
  多省 = 一张图片版；图片开关关闭或生图端点不可用时自动回退纯文字
- **调价提前提醒**：距下次调价窗口还剩 N 天（可配 0-3）时额外发送文字提醒
- **每月走势推送**：每月 1 日向推送目标发送上一月每日油价折线图（可开关）
- **油价统计页**：按省份/油号/时间范围生成折线图与汇总表（WebUI 内嵌，跟随页面明暗主题）
- **目标绑定限额**：每个私聊/群聊推送目标可单独设置每日查询上限，未设置用全局默认
- **WebUI 设置页**：省份/油号多选、推送时间与频率、提前提醒、图片开关与生图模型、目标与限额、
  appKey 管理（今日油价 + 油价统计 双 tab）

## 安装

将本仓库克隆到 AstrBot 插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/momolee-gg/astrbot_plugin_oil_price.git
```

或在 WebUI「插件」页通过仓库地址安装。安装后：

1. 在侧边栏「今日油价 → 系统配置 → 接口配置」填写 ShowAPI appKey（[控制台获取](https://www.showapi.com/)）
2. 勾选推送省份、配置推送时间与目标
3. 保存即生效（appKey 就绪后推送定时器自动启动）

## 指令

| 指令 | 说明 |
|------|------|
| `/油价 [省份]` | 文字版；不带省份时播报全部已选省份 |
| `/今日油价 [省份]` | 图片版（AI 生图，随机风格）；不带省份时多省合一图 |
| `/看看油价 [省份] [油号]` | 单油种大字卡片 |
| `/我要加油 <升数> [省份] [油号]` | 加油结算单 |
| `/油价设置 <省份> <油号>` | 快捷记住省份/油号 |
| `/调价窗口` | 下次调价窗口倒计时 |
| `/油价推送测试` | 向推送目标立即执行一次推送 |

## 文档

- [项目简介](https://github.com/momolee-gg/astrbot_plugin_oil_price/blob/main/docs/01-项目简介.md)
- [项目需求 v1.0](https://github.com/momolee-gg/astrbot_plugin_oil_price/blob/main/docs/02-项目需求-v1.0.md)
- [开发文档](https://github.com/momolee-gg/astrbot_plugin_oil_price/blob/main/docs/03-项目开发文档.md)
- [测试要求](https://github.com/momolee-gg/astrbot_plugin_oil_price/blob/main/docs/04-项目测试要求.md)
- [测试案例 v1.0.0](https://github.com/momolee-gg/astrbot_plugin_oil_price/blob/main/docs/05-测试案例-v1.0.0.md)
- [测试报告 v1.0.0](https://github.com/momolee-gg/astrbot_plugin_oil_price/blob/main/docs/reports/测试报告-v1.0.0.md)

## 环境

- AstrBot >= 4.29.1（已在 4.29.1 实测）
- Python >= 3.12
- 依赖见 `requirements.txt`（Pillow 出折线图/单油卡；每日海报走 bot 已配置模型的生图端点；运行数据写在 AstrBot `data/plugin_data/` 下）

## 安全说明

- appKey 仅保存在 AstrBot 配置中，请勿提交到 Git
- 查询配额按「推送目标 + 用户」维度独立计量，详见开发文档
- 发现安全问题请参考 `SECURITY.md`
