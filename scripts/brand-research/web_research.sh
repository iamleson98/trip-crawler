#!/usr/bin/env bash
# Deep research via web search: vexere.com features + top brand support phones.
set -u
OUT=/home/z/my-project/research/websearch
mkdir -p "$OUT"

UA_JSON='{"num": 8}'

run() {
  local name="$1"; shift
  local query="$1"; shift
  if [ -s "$OUT/$name.json" ]; then
    echo "skip $name (exists)"
    return
  fi
  z-ai function -n web_search -a "{\"query\": \"$query\", \"num\": 8}" -o "$OUT/$name.json" >/dev/null 2>&1
  local sz=$(wc -c < "$OUT/$name.json" 2>/dev/null || echo 0)
  echo "done $name ($sz bytes)"
}

# ---------- Batch 1: vexere platform features ----------
run feat_services        "vexere.com dịch vụ đặt vé xe khách vé tàu hỏa vé máy bay"
run feat_motorbike       "vexere thuê xe máy"
run feat_train           "vexere bán vé tàu hỏa SE genomes đường sắt"
run feat_flight          "vexere vé máy bay nội địa"
run feat_app             "vexere ứng dụng di động iOS Android tải app"
run feat_promo           "vexere mã giảm giá khuyến mãi FlashSale ưu đãi"
run feat_payment         "vexere thanh toán Momo ZaloPay VNPay thẻ visa"
run feat_hotline         "vexere tổng đài hỗ trợ 1900 8888 43 hotline"
run feat_agent           "vexere đại lý affiliate chương trình hợp tác"
run feat_charter         "vexere thuê xe trọn gói charter du lịch"
run feat_company         "vexere công ty VeXeRe Vietnam thành lập funding"
run feat_tet             "vexere vé xe Tết 2027 đặt vé sớm"
run feat_ebooking        "vexere e-ticket vé điện tử lên xe không cần in"
run feat_busstop         "vexere bến xe điểm đón trả bus stop thông tin"

echo "=== batch 1 done ==="

# ---------- Batch 2: top-30 brand support hotlines ----------
run phone_phuong_trang   "Phương Trang FUTA Bus hotline số điện thoại tổng đài"
run phone_dien_linh      "Điền Linh Limousine hotline số điện thoại"
run phone_hoang_cong     "Hoàng Công Limousine hotline số điện thoại"
run phone_hai_au         "Hải Âu limousine hotline số điện thoại"
run phone_hoang_long     "Hoàng Long bus hotline số điện thoại tổng đài"
run phone_viet_lao       "Việt Lào Đà Nẵng bus hotline số điện thoại"
run phone_tan_quang_dung "Tân Quang Dũng limousine hotline số điện thoại"
run phone_hk_buslines    "HK BUSLINES limousine hotline số điện thoại"
run phone_cu_cung        "Cúc Tùng bus hotline số điện thoại"
run phone_lien_hung      "Liên Hưng bus hotline số điện thoại tổng đài"
run phone_tan_aba        "Tân Anpha bus Tân Aba hotline số điện thoại"
run phone_sao_viet       "Sao Việt bus hotline số điện thoại"
run phone_g8             "G8 Open Tour limousine hotline số điện thoại"
run phone_hm_happycar    "HM Happycar limousine hotline số điện thoại"
run phone_hoang_ha       "Hoàng Hà Phú Thọ bus hotline số điện thoại"
run phone_hai_van        "Hải Vân Travel bus hotline số điện thoại"
run phone_vip_phuong_huy "Vip Phương Huy Luxury hotline số điện thoại"
run phone_anh_huy        "Anh Huy Đất Cảng Hải Phòng hotline số điện thoại"
run phone_thanh_travel   "Thành Travel Car hotline số điện thoại"
run phone_thuan_phat     "Thuận Phát Limousine hotline số điện thoại"
run phone_ket_doan       "Kết Đoàn Bus hotline số điện thoại"
run phone_tan_kim_chi    "Tân Kim Chi bus hotline số điện thoại"
run phone_khanh_phong    "Khanh Phong bus hotline số điện thoại"
run phone_khang_limo     "Khang Limousine hotline số điện thoại"
run phone_nguyen_gia     "Nguyễn Gia Limousine Hải Phòng hotline số điện thoại"
run phone_dinh_nhan      "Đình Nhân bus hotline số điện thoại"
run phone_sao_vang       "Sao Vàng Golink bus hotline số điện thoại"
run phone_phong_khanh    "Phong Khánh bus hotline số điện thoại"
run phone_s_trip         "S Trip Việt Nam limousine hotline số điện thoại"
run phone_hoang_phuong   "Hoàng Phương Limousine hotline số điện thoại"

echo "=== batch 2 done ==="
ls -la "$OUT" | wc -l
