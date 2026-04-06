# OpenClaw Manager

A lightweight multi-user OpenClaw instance management platform.

一个轻量级的 OpenClaw 多用户实例管理平台。

---

## 🚀 Overview | 项目简介

OpenClaw Manager is designed to provide a scalable way to run multiple isolated OpenClaw instances, one per user, with centralized management.

OpenClaw Manager 用于在服务器上为多个用户提供隔离的 OpenClaw 实例，实现统一管理与运维。

---

## ✨ Features | 功能特点

- Per-user OpenClaw container isolation  
  每用户独立 OpenClaw 容器实例

- Script-based user provisioning  
  基于脚本的用户创建与管理

- Nginx reverse proxy with authentication  
  基于 Nginx 的反向代理与访问控制

- Secure multi-layer access control  
  多层安全机制（Basic Auth + Token + Device Approve）

- Ready for OAuth2 integration (future)  
  预留 OAuth2（统一认证）接入能力

- Extensible for quota / rate limiting  
  支持后续接入限流与配额控制（API Gateway）

---

## 📁 Project Structure | 项目结构

openclaw-manager/
├── templates/        # 模板（docker-compose / config）
├── scripts/          # 自动化脚本
├── nginx/            # 反向代理
│   ├── conf.d/
│   └── htpasswd/
├── logs/             # 日志目录
├── docs/             # 文档
├── README.md
└── .gitignore

---

## 🔐 Security Model | 安全设计

- One OpenClaw instance per user  
  每用户独立实例（避免权限污染）

- Nginx Basic Authentication  
  第一层认证（账号密码）

- OpenClaw token protection  
  第二层认证（API Token）

- Device approval mechanism  
  第三层认证（设备授权）

---

## ⚠️ Important Notes | 注意事项

- Do NOT commit runtime data (users/, tokens, configs)  
  禁止提交运行时数据（users/、token、配置文件）

- Each user instance must be fully isolated  
  每个用户实例必须完全隔离

- This project is NOT a multi-tenant OpenClaw modification  
  本项目不是对 OpenClaw 的多租户改造，而是外部管理方案

---

## 🛠 Roadmap | 后续规划

- [ ] Automated user provisioning
- [ ] Port allocation system
- [ ] Nginx dynamic routing
- [ ] OAuth2 / SSO integration
- [ ] API Gateway for quota control

---

## 📌 License

MIT

---

## 👤 Author

Maintained for internal multi-user OpenClaw deployment.

面向多用户 OpenClaw 部署场景设计。
