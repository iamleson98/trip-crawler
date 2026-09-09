#!/usr/bin/env python3
"""Merge all research into the final datasets used by the Excel report builder:
- research/final_brands.json   : 306 brands, all fields
- research/final_reviews.json  : all customer reviews
- research/final_features.json : vexere platform feature inventory
"""
import json

INV = json.load(open("/home/z/my-project/research/brand_matches.json"))
PAGE = json.load(open("/home/z/my-project/research/brands_data.json"))

# slug overrides / manual resolutions discovered during probing
SLUG_FIX = {
    "Anh Huy Đất Cảng": "xe-anh-huy-dat-cang-xe-khach",
    "Quốc Phương Car": None,  # vexere page is broken (redirect loop)
    "Cát Bà Limousine": None,  # no dedicated page found
}
# shared pages (same operator, two display names in our crawl)
SHARED_NOTE = {
    "G8 OPEN TOUR": "same operator as G8 SAPA OPEN TOUR — one vexere page",
}

# Curated official support phones from web search (top brands)
WEB_PHONES = {
    "Phương Trang": {"phones": "1900 6067 (đặt vé / CSKH); 1900 6667 (khách); 1900 6767 (hàng)", "website": "futabus.vn", "source": "futabus.vn, fptshop.com.vn"},
    "Điền Linh Limousine": {"phones": "1900 1191 (HCM); 02633 68 68 68 (Lâm Đồng); 02743 728 728 (Bình Dương); 1900 25 25 00 (Hà Nội)", "website": "xedienlinh.com", "source": "xedienlinh.com, facebook.com/xedienlinh"},
    "Anh Huy Đất Cảng": {"phones": "1900 1876 (xe khách); 0813 582 582 / *1516 (limousine); 0911 645 999; 0225 3822 622", "website": "anhhuydatcang.com", "source": "anhhuydatcang.com, Facebook fanpage"},
    "Anh Huy Travel": {"phones": "0911 863 911; 1900 2811 (Hà Nội)", "website": "anhhuy.com.vn", "source": "vexere.com brand page, Facebook"},
    "Hoàng Công": {"phones": "1900 0392; 0783 36 0379", "website": "—", "source": "ivexe.vn"},
    "Hải Âu": {"phones": "0225 3717 717", "website": "haiaubus.vn", "source": "haiaubus.vn (chính thức)"},
    "Hoàng Long - Hà Nội": {"phones": "0243 928 2828 (đặt vé); 0243 994 6617; 0225 3920 920 (tổng đài)", "website": "hoanglongasia.com", "source": "hoanglongasia.com, queenbus.com.vn"},
    "Việt Lào (Đà Nẵng)": {"phones": "0978 583 910 (7:30–17:00); 0935 91 7677 (7:30–22:30); Zalo 0914 425 788", "website": "vietlaobus.com", "source": "vietlaotravelbus.com.vn, danava.com.vn"},
    "Cúc Tùng": {"phones": "1900 6606 (hotline duy nhất); 028 9998 4199 (đặt vé)", "website": "xecuctunglimousine.com", "source": "xecuctunglimousine.com, Facebook"},
    "Tân Quang Dũng": {"phones": "0909 053 053 (HCM); 1900 6053 (Quảng Trị); 0947 053 053 (Bình Phước); 0707 053 053 (Đà Nẵng)", "website": "tanquangdung.com", "source": "tanquangdung.com, vexere.com"},
    "HK BUSLINES": {"phones": "1900 996 678 (1.000đ/phút); 1900 0179 (đặt vé nhanh 24/7)", "website": "—", "source": "vivutoday.com"},
    "Liên Hưng": {"phones": "028 3535 7525 (VP Q5 HCM); 1900 0152", "website": "—", "source": "mia.vn, vivutoday.com"},
    "Tân Aba": {"phones": "1900 599 997", "website": "—", "source": "vexekhachvn.com"},
    "Sao Việt": {"phones": "0243 9922 555 / 090 222 7070 (đặt vé 7:00–22:00); 0243 8645 758 (gửi hàng)", "website": "xekhachsaoviet.com", "source": "xekhachsaoviet.com, xesaoviet.com.vn"},
    "G8 SAPA OPEN TOUR": {"phones": "0965 611 611; 0961 615 611", "website": "g8opentour.vn", "source": "g8opentour.vn, Facebook"},
    "HM Happycar": {"phones": "1900 8167; 0236 653 7999 (Đà Nẵng); 0702 48 58 68", "website": "—", "source": "Facebook HM HappyCar"},
    "Hải Vân Travel (Đà Nẵng)": {"phones": "1900 6763 (hành lý & hàng hóa)", "website": "haivan.com", "source": "haivan.com"},
    "Vip Phương Huy Luxury": {"phones": "1900 299 925 (hotline); 0225 3925 925 (tổng đài)", "website": "xephuonghuy.com.vn", "source": "xephuonghuy.com.vn"},
    "Thuận Phát Limousine": {"phones": "0762 570 570 (hotline & Zalo); 1900 996 633", "website": "xeghepthuanphat.com", "source": "xeghepthuanphat.com"},
    "Kết Đoàn Bus": {"phones": "1900 3239; 0915 289 431 (tổng đài); 0965 221 221 (Hải Phòng–Sơn Tây)", "website": "ketdoanbus.com", "source": "ketdoanbus.com"},
    "Kết Đoàn Travel": {"phones": "1900 3239; 0915 289 431", "website": "ketdoanbus.com", "source": "ketdoanbus.com"},
    "Tân Kim Chi": {"phones": "1900 5151 (đặt vé toàn quốc); 0707 597 597 (gửi hàng)", "website": "tankimchi.vn", "source": "tankimchi.vn (chính thức)"},
    "Khanh Phong": {"phones": "0258 3823 999; 0258 3524 998 (Nha Trang)", "website": "xekhanhphongvip.vn", "source": "xekhanhphongvip.vn (chính thức)"},
    "Nguyễn Gia Limousine (Hải Phòng)": {"phones": "1900 25 26 26", "website": "nguyengialimousine.vn", "source": "nguyengialimousine.vn (chính thức)"},
    "Đình Nhân": {"phones": "1900 25 25 00 (24/7); 0905 345 345 / 0905 123 123 (Đà Nẵng)", "website": "—", "source": "Facebook, ivexe.vn"},
    "Sao Vàng Golink": {"phones": "1900 0258", "website": "saovanggolink.vn", "source": "saovanggolink.vn (chính thức)"},
    "Phong Khánh": {"phones": "1900 88 86 84 (đặt vé tại Sapa)", "website": "—", "source": "vexere.com brand page"},
    "S Trip Việt Nam": {"phones": "1900 888 828", "website": "—", "source": "xehalong.vn"},
    "Hoàng Phương Limousine (Hải Phòng)": {"phones": "1900 1988 (hotline); 0888 827 827; 0931 595 988", "website": "hoangphuonglimousine.com", "source": "hoangphuonglimousine.com, redbus.vn"},
    "Thăng Long Hà Nội Limousine": {"phones": "024 3928 2828", "website": "—", "source": "vexere.com, hoanglongasia.com"},
}

VEXERE_SUPPORT = "1900 8888 43 (7:00–22:00) — đặt vé mọi nhà xe; cs@vexere.com; 1900 88 86 84 (CSKH sàn TMĐT)"

# ---------- build brand rows ----------
brands, reviews = [], []
for b in INV:
    name = b["name"]
    slug = b.get("slug") or SLUG_FIX.get(name, b.get("slug"))
    if name in SLUG_FIX:
        slug = SLUG_FIX[name]
    page = PAGE.get(slug) if slug else None
    web = WEB_PHONES.get(name)
    own = ""
    if page and page.get("own_phones"):
        own = "; ".join(f"{num} ({loc})" for num, loc in list(page["own_phones"].items())[:4])
    row = {
        "name": name,
        "trips": b["n_trips"],
        "routes_n": b["n_routes"],
        "routes": ", ".join(b["routes"][:6]) + ("…" if b["n_routes"] > 6 else ""),
        "veh": ", ".join(b["veh_types"][:3]),
        "min_price": b["min_price"],
        "max_price": b["max_price"],
        "rating": float(page["rating"]) if page and page.get("rating") not in (None, "0") else None,
        "rating_count": int(page["rating_count"]) if page and page.get("rating_count") else None,
        "price_range": (page or {}).get("price_range"),
        "vexere_hotline": (page or {}).get("telephone"),
        "own_phones": own,
        "address": (page or {}).get("address") or "; ".join(((page or {}).get("offices") or [])[:2]),
        "url": f"https://vexere.com/vi-VN/{slug}" if slug else "",
        "web_phones": web["phones"] if web else "",
        "web_website": web["website"] if web else "",
        "web_source": web["source"] if web else "",
        "n_reviews_page": len((page or {}).get("reviews", [])) if page else 0,
        "note": SHARED_NOTE.get(name, ""),
        "status": "OK" if slug else "no vexere page found",
    }
    brands.append(row)
    if page:
        for r in page.get("reviews", []):
            reviews.append({
                "brand": (page.get("product_name") or name).strip(),
                "date": r.get("date"),
                "author": (r.get("author") or "").strip(),
                "stars": r.get("rating"),
                "text": (r.get("text") or "").replace("\n", " ").strip(),
            })

brands.sort(key=lambda x: -x["trips"])
json.dump(brands, open("/home/z/my-project/research/final_brands.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump(reviews, open("/home/z/my-project/research/final_reviews.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

n_rating = sum(1 for b in brands if b["rating"])
n_cnt = sum(1 for b in brands if b["rating_count"] and b["rating_count"] > 1)
n_own = sum(1 for b in brands if b["own_phones"])
n_web = sum(1 for b in brands if b["web_phones"])
n_url = sum(1 for b in brands if b["url"])
print(f"brands: {len(brands)} | with rating: {n_rating} | with rating_count>1: {n_cnt}")
print(f"with own phones: {n_own} | with web phones: {n_web} | with vexere URL: {n_url}")
print(f"reviews: {len(reviews)} | distinct brands in reviews: {len(set(r['brand'] for r in reviews))}")
print(f"total rating_count sum: {sum(b['rating_count'] or 0 for b in brands):,}")
