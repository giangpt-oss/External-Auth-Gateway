---
version: "alpha"
name: "External Auth Gateway"
description: "Cổng liên kết & đồng bộ Odoo Online Enterprise — quản lý tài khoản Google Workspace và Telegram Bot cho nhân viên."
omitted: []
colors:
  # ── Surface & Backgrounds ──
  bg-body: "#f8fafc"
  bg-surface: "#ffffff"
  bg-surface-subtle: "#f1f5f9"
  bg-surface-well: "#e2e8f0"
  bg-glass: "rgba(255, 255, 255, 0.65)"
  bg-glass-border: "rgba(255, 255, 255, 0.85)"
  # ── Text ──
  text-main: "#0f172a"
  text-secondary: "#475569"
  text-muted: "#64748b"
  text-inverse: "#ffffff"
  text-accent: "#0f766e"
  # ── Borders ──
  border-subtle: "#e2e8f0"
  border-main: "#cbd5e1"
  border-strong: "#94a3b8"
  border-focus: "#0f766e"
  # ── Primary — Emerald ──
  primary-50: "#ecfdf5"
  primary-100: "#d1fae5"
  primary-200: "#a7f3d0"
  primary: "#0f766e"
  primary-hover: "#115e59"
  primary-active: "#134e4a"
  primary-glow: "rgba(15, 118, 110, 0.15)"
  primary-focus-ring: "rgba(15, 118, 110, 0.25)"
  # ── Status Semantics ──
  success: "#047857"
  success-bg: "#ecfdf5"
  success-border: "#86efac"
  warning: "#b45309"
  warning-bg: "#fffbeb"
  warning-border: "#fde68a"
  danger: "#b91c1c"
  danger-hover: "#991b1b"
  danger-bg: "#fef2f2"
  danger-border: "#fecaca"
  neutral: "#475569"
  neutral-bg: "#f8fafc"
  neutral-border: "#cbd5e1"
  info: "#0369a1"
  info-bg: "#eff6ff"
  info-border: "#bfdbfe"
typography:
  display-lg:
    fontFamily: "Inter"
    fontSize: "2rem"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  display-md:
    fontFamily: "Inter"
    fontSize: "1.625rem"
    fontWeight: 800
    lineHeight: 1.25
    letterSpacing: "-0.02em"
  headline-lg:
    fontFamily: "Inter"
    fontSize: "1.25rem"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  body-md:
    fontFamily: "Inter"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
    color: "{text-main}"
  body-sm:
    fontFamily: "Inter"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.55
    color: "{text-secondary}"
  label-caps:
    fontFamily: "Inter"
    fontSize: "0.725rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "0.04em"
    textTransform: "uppercase"
    color: "{text-muted}"
  code-sm:
    fontFamily: "JetBrains Mono"
    fontSize: "0.825rem"
    fontWeight: 500
    lineHeight: 1.4
rounded:
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  full: "9999px"
spacing:
  xs: "0.25rem"
  sm: "0.5rem"
  md: "1rem"
  lg: "1.5rem"
  xl: "2rem"
  xxl: "3rem"
motion:
  fast:
    duration: "120ms"
    timingFunction: "cubic-bezier(0.4, 0, 0.2, 1)"
  normal:
    duration: "200ms"
    timingFunction: "cubic-bezier(0.4, 0, 0.2, 1)"
  spring:
    duration: "300ms"
    timingFunction: "cubic-bezier(0.34, 1.56, 0.64, 1)"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.text-inverse}"
    borderColor: "{colors.primary}"
    rounded: "{rounded.md}"
    padding: "0.625rem 1.125rem"
    size: "42px"
    typography: "{typography.label-caps}"
    shadow: "{colors.primary-glow}"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    borderColor: "{colors.primary-hover}"
    shadow: "rgba(15, 118, 110, 0.25)"
  button-secondary:
    backgroundColor: "{colors.bg-surface}"
    textColor: "{colors.text-main}"
    borderColor: "{colors.border-main}"
    rounded: "{rounded.md}"
    padding: "0.625rem 1.125rem"
    size: "42px"
    typography: "{typography.label-caps}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text-secondary}"
    borderColor: "transparent"
    rounded: "{rounded.md}"
    padding: "0.4rem 0.875rem"
    size: "34px"
    typography: "{typography.label-caps}"
  card:
    backgroundColor: "{colors.bg-surface}"
    borderColor: "{colors.border-subtle}"
    rounded: "{rounded.xl}"
    padding: "1.5rem"
    shadow: "{colors.shadow-md}"
  badge:
    rounded: "{rounded.full}"
    padding: "0.3rem 0.75rem"
    size: "32px"
    typography: "{typography.label-caps}"
  input:
    backgroundColor: "{colors.bg-surface}"
    borderColor: "{colors.border-main}"
    rounded: "{rounded.md}"
    padding: "0.725rem 1rem"
    size: "46px"
    typography: "{typography.body-md}"

## Overview
Cổng Xác Thực & Đồng Bộ — giao diện quản lý liên kết Odoo với Google Workspace và Telegram Bot. Thiết kế theo hướng "Minimalism + Swiss Style + Glassmorphism" dành cho sản phẩm công nghệ / công cụ dev (SaaS, Identity & Access Management). Một section trên mỗi màn hình ở desktop (1440x900), lề trái phải bằng nhau, thanh nav cố định, footer đóng màn hình duy nhất.

## Colors
Bảng màu căn cứ vào nền xám lạnh (slate-white), chữ đen đậm (deep ink), đỏ nhấn emerald cho hành động chính, và một màu ngõ cảnh báo (warning) / nguy hiểm (danger) chuẩn WCAG AA. Màu nền body (#f8fafc) mềm hơn trắng tinh khiết, tạo lớp lớp-up mượt mà với các card trắng (bg-surface).

## Typography
Hệ chữ Inter cho hiển thị và nội dung, JetBrains Mono cho mã/mono. Phân cấp rõ ràng: display-lg (h1 trang chính), headline-lg (tiêu đề card), body-md (nội dung), label-caps (nhãn viết hoa in đậm), code-sm (mã ngắn).

## Layout
Container max-width 1100px, padding đối xứng trái phải. Grid 2 cột ở desktop (≥768px), 1 cột ở mobile. Các card cách đều gap 1.5rem. Section có card-section làm phân cách bằng đường nét ngang.

## Elevation & Depth
Hệ shadow từ xs→xl, cùng shadow-glow cho primary và avatar lớn. Card nổi nhẹ (shadow-md), càng nổi khi hover (shadow-lg).

## Shapes
Border-radius từ xs (6px) đến full (9999px) cho badge. Card dùng xl (16px), nút dùng md (12px), avatar lớn dùng lg (16px).

## Motion & Micro-interaction
Hiệu ứng nhanh 120ms, thường 200ms, spring 300ms. Badge hoạt động pulse liên tục. Nút co nhẹ khi active (translateY 0.5px). Focus ring visible cho điều hướng bàn phím. Tùy chọn prefers-reduced-motion được tôn trọng.

## Voice & Content Strategy
Ngôn ngữ tiếng Việt, giọng điệu chuyên nghiệp, gần gwürdig, không sử dụng emoji làm biểu tượng (chỉ dùng SVG: Heroicons/Lucide). Tất cả phần tử có thể nhấn phải có cursor-pointer. Tóm tắt trạng thái hệ thống ở trên cùng (status grid).

## Components
- **button-primary / button-secondary / button-ghost**: ba biến thể hành động, focus ring, shadow glow.
- **card**: nền trắng, viền mỏng, bo tròn lớn, sombra nhẹ.
- **badge**: bo tròn vòng, màu ngữ nghĩa (success/warning/danger/info/secondary), không dựa vào màu đơn lẻ để truyền ý nghĩa.
- **input**: ô nhập đầy rộng, focus ring primary, placeholder mềm.

## Do's and Don'ts
- ✅ Dùng SVG thay emoji làm icon.
- ✅ Mọi phần tử nhấn phải có cursor-pointer và focus state rõ ràng.
- ✅ Độ tương phản văn bản light mode tối thiểu 4.5:1 (WCAG AA).
- ✅ Responsive tại 375px, 768px, 1024px, 1440px.
- ✅ Văn bản, chip, badge tràn dòng không bị cắt labels.
- ❌ Không dùng gradient tím/xanh nhánh kiểu AI generic.
- ❌ Không dùng hoạt ảnh mạnh mẽ trên trang doanh nghiệp / công nghệ quản lý.
- ❌ Không để màu đơn lẻ mang toàn bộ ý nghĩa của badge.
