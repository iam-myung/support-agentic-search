---
workflow: product-launch-video
flow: automation
storyboard: no
message: "GitHub 项目说明：企业客服智能体是什么、架构如何设计、业务如何跑、数据如何流动"
destination: youtube
aspect: 1920x1080
language: zh
length: 75s
angle: product-demo-walkthrough
style_preset: code-editorial
narration: no
---

## Intent

面向 GitHub README / 作品集：用静音动画把仓库讲清楚——解决什么问题、五层架构、一次回合如何跑、本地与网搜数据如何汇入、来源判定与治理边界。不以 UI 点击演示为主戏。

## Customizations

- 全中文；架构与数据流优先于登录/光标操作
- 样本问题贯穿：密码重置是否需要 MFA（本地政策 + 公开实践 → MIXED）
- 综合是循环收束阶段，不是第三工具；白名单仅 local_retrieve / web_search

## Notes

- 预览优先，暂不渲染 MP4
- 勿暴露 API Key / 原始 CoT / Prompt
