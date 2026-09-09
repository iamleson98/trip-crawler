#!/usr/bin/env python3
"""seed_booking_db.py — Seed the booking-app SQLite database from the enriched
VeXeRe N×N crawl Excel (vexere_nxn_trips_enriched.xlsx).

What it does
------------
1. Reads every (from, to) pair sheet of the enriched Excel (header row 4,
   data from row 5). Each data row is one dated departure fare.
2. Creates the FULL schema (all tables, exactly as given by the booking app's
   DDL) in a fresh SQLite file, then populates the dimension tables:

     Operator            → brand          (dedup by operator name)
     (operator, pair)    → route          (start/end = city ids from the
                                           VIETNAMESE_CITIES list; e.g. sheet
                                           NhaTrang → 'khanh-hoa',
                                           SaPa → 'lao-cai', DaLat → 'lam-dong')
     (operator, veh,
      total seats)       → bus_layout     (bonus: preserves vehicle info)
     trip points         → address        (dedup per brand by normalized name
                                          + 2 km geo-cluster; coords = the
                                          most frequently seen pair;
                                          province/district/ward filled via
                                          https://datxevui.com/api/places/reverse)
     rows grouped by
      (route, dep time,
       bus layout)       → schedule       (ONE recurring service per key —
                                           rows with per-date time/point
                                          variations merge into it; canonical
                                          point list = most frequent variant,
                                          stop times = modal offset;
                                          effective_from/to, days_of_week,
                                          base_price_adult = min list price)
     points per schedule → schedule_point (kind = 'pickup' (boarding, T<=0) /
                                           'middle' (boarding, T>0) / 'drop'
                                           (alighting); arrival_time = 'T0',
                                           'T+30min', 'T-120min', ...)

Duplicate prevention (v2): every table is deduplicated on its logical
business key (see the rules above build_model) and the database itself
enforces them with UNIQUE indexes (route, bus_layout, address, schedule,
schedule_point) — any future duplicate insert fails loudly. verify_db()
runs a full duplicate audit over every seeded table and the script exits
non-zero if any check fails.

3. Reverse geocoding: unique (lat, lon) coordinates are looked up ONCE each
   (cached in a JSON file so re-runs are free) against
       https://datxevui.com/api/places/reverse?lat={lat}&lon={lon}&limit=1
   The API returns the nearest place with `province` and a ward-level
   `district` ("phuong ..."/"xa ..." — post-2025 commune units):
       province ← api 'province'          (title-cased)
       ward     ← api 'district' value when it is a phường/xã
       district ← api 'district' value when it is a quận/huyện
   When the API fails / returns nothing, the VeXeRe district label from the
   Excel cell ("[Hoàng Mai]") is used as a fallback for `district`.
   Points with lat=lon=0 (district-wide service zones) are never geocoded.

Data-quality guard
------------------
The current Excel still contains the route-level point-aggregation bug
(e.g. route 7275428 "Anh Huy Đất Cảng" shows 1,953 pickup points while the
website lists 1). Rows whose declared "# Pickup"/"# Dropoff" exceeds
--max-declared-points (default 30) keep their schedule (time/price/vehicle
are per-trip and valid) but their point lists are NOT trusted and produce no
schedule_points / addresses. Re-run with a corrected Excel to fix.

Usage
-----
    python3 seed_booking_db.py \
        [--excel PATH] [--db PATH] [--cache PATH] \
        [--workers N] [--max-declared-points N] [--no-geocode] [--fresh]

Output: SQLite DB + reverse-geocode cache + a verification report.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import requests

# --------------------------------------------------------------------------
# Schema (verbatim DDL of the booking app — all tables, incl. empty ones)
# --------------------------------------------------------------------------
SCHEMA_SQL = r"""
CREATE TABLE "address" ( "id" uuid_text NOT NULL PRIMARY KEY, "brand_id" uuid_text NOT NULL, "name" varchar(255) NOT NULL, "address" text NULL, "lat" double NOT NULL, "lon" double NOT NULL, "province" text NULL, "district" text NULL, "ward" text NULL, "created_at" text NOT NULL, "updated_at" text NOT NULL, FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "audit_log" ( "id" uuid_text NOT NULL PRIMARY KEY, "actor_type" varchar(50) NULL, "actor_id" uuid_text NULL, "action" varchar(50) NOT NULL, "target_type" varchar(50) NULL, "target_id" uuid_text NULL, "metadata" text NULL, "ip" varchar(45) NULL, "user_agent" text NULL, "created_at" text NOT NULL );
CREATE TABLE "booking" ( "id" uuid_text NOT NULL PRIMARY KEY, "code" varchar(20) NOT NULL UNIQUE, "user_id" uuid_text NULL, "guest_name" varchar(255) NULL, "guest_phone" varchar(20) NULL, "guest_email" varchar(255) NULL, "trip_session_id" uuid_text NOT NULL, "boarding_point_id" uuid_text NULL, "dropping_point_id" uuid_text NULL, "adult_count" integer NOT NULL DEFAULT 1, "child_count" integer NOT NULL DEFAULT 0, "subtotal" integer NOT NULL DEFAULT 0, "discount" integer NOT NULL DEFAULT 0, "fees" integer NOT NULL DEFAULT 0, "total" integer NOT NULL DEFAULT 0, "currency" varchar(3) NOT NULL DEFAULT 'VND', "status" varchar(30) NOT NULL DEFAULT 'pending', "payment_method" varchar(30) NULL, "campaign_applied_id" uuid_text NULL, "expires_at" text NULL, "contact_name" varchar(255) NULL, "contact_phone" varchar(20) NULL, "contact_email" varchar(255) NULL, "created_at" text NOT NULL, "updated_at" text NOT NULL, "dropoff_address" text NULL, "dropoff_lat" double NULL, "dropoff_lon" double NULL, "dropoff_name" varchar(255) NULL, "pickup_address" text NULL, "pickup_lat" double NULL, "pickup_lon" double NULL, "pickup_name" varchar(255) NULL, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE SET NULL ON UPDATE CASCADE, FOREIGN KEY ("trip_session_id") REFERENCES "trip_session" ("id") ON DELETE RESTRICT ON UPDATE CASCADE, FOREIGN KEY ("boarding_point_id") REFERENCES "pickup_point" ("id") ON DELETE SET NULL ON UPDATE CASCADE, FOREIGN KEY ("dropping_point_id") REFERENCES "pickup_point" ("id") ON DELETE SET NULL ON UPDATE CASCADE, FOREIGN KEY ("campaign_applied_id") REFERENCES "campaign" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "booking_seat" ( "id" uuid_text NOT NULL PRIMARY KEY, "booking_id" uuid_text NOT NULL, "seat_id" uuid_text NOT NULL, "passenger_name" varchar(255) NULL, "passenger_age" smallint NULL, "passenger_type" varchar(10) NULL, "price" integer NOT NULL DEFAULT 0, "created_at" text NOT NULL, FOREIGN KEY ("booking_id") REFERENCES "booking" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("seat_id") REFERENCES "seat" ("id") ON DELETE RESTRICT ON UPDATE CASCADE );
CREATE TABLE "brand" ( "id" uuid_text NOT NULL PRIMARY KEY, "slug" varchar(120) NOT NULL UNIQUE, "name" varchar(255) NOT NULL, "logo_url" varchar(500) NULL, "description" text NULL, "contact_phone" varchar(20) NULL, "contact_email" varchar(255) NULL, "rating" double NULL, "status" varchar(30) NOT NULL DEFAULT 'active', "accent_color" varchar(9) NULL, "total_trips" integer NOT NULL DEFAULT 0, "created_at" text NOT NULL, "updated_at" text NOT NULL );
CREATE TABLE "bus_layout" ( "id" uuid_text NOT NULL PRIMARY KEY, "brand_id" uuid_text NULL, "name" varchar(255) NULL, "vehicle_type" varchar(30) NULL, "total_seats" smallint NULL, "layout_data" text NULL, "created_at" text NOT NULL, "updated_at" text NOT NULL, FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "campaign" ( "id" uuid_text NOT NULL PRIMARY KEY, "brand_id" uuid_text NOT NULL, "code" varchar(50) NOT NULL UNIQUE, "discount_type" varchar(10) NOT NULL, "discount_value" bigint NOT NULL, "max_uses" bigint NULL, "used_count" bigint NOT NULL DEFAULT 0, "starts_at" text NULL, "ends_at" text NULL, "status" varchar(30) NOT NULL DEFAULT 'active', "created_at" text NOT NULL, "updated_at" text NOT NULL, FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "chat_assignment" ( "id" uuid_text NOT NULL PRIMARY KEY, "channel_id" uuid_text NOT NULL, "employee_id" text NULL, "assigned_at" text NOT NULL, "unassigned_at" text NULL, FOREIGN KEY ("channel_id") REFERENCES "chat_channel" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "chat_channel" ( "id" uuid_text NOT NULL PRIMARY KEY, "user_id" uuid_text NOT NULL, "brand_id" uuid_text NULL, "topic" varchar(255) NULL, "status" varchar(30) NOT NULL DEFAULT 'open', "priority" varchar(10) NOT NULL DEFAULT 'normal', "last_message_at" text NULL, "last_message_preview" varchar(500) NULL, "unread_user" integer NOT NULL DEFAULT 0, "unread_employee" integer NOT NULL DEFAULT 0, "created_at" text NOT NULL, "closed_at" text NULL, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "chat_channel_member" ( "id" uuid_text NOT NULL PRIMARY KEY, "channel_id" uuid_text NOT NULL, "user_id" uuid_text NOT NULL, "role" varchar(20) NOT NULL, "joined_at" text NOT NULL, "left_at" text NULL, FOREIGN KEY ("channel_id") REFERENCES "chat_channel" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "chat_message" ( "id" uuid_text NOT NULL PRIMARY KEY, "channel_id" uuid_text NOT NULL, "sender_type" varchar(20) NOT NULL, "sender_id" uuid_text NULL, "content" text NULL, "kind" varchar(20) NOT NULL DEFAULT 'text', "attachments" text NULL, "status" varchar(20) NOT NULL DEFAULT 'sent', "client_msg_id" varchar(100) NULL, "created_at" text NOT NULL, FOREIGN KEY ("channel_id") REFERENCES "chat_channel" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "discount_program" ( "id" uuid_text NOT NULL PRIMARY KEY, "brand_id" uuid_text NULL, "name" varchar(255) NOT NULL, "description" text NULL, "discount_type" varchar(10) NOT NULL, "discount_value" integer NOT NULL, "starts_at" text NULL, "ends_at" text NULL, "status" varchar(30) NOT NULL DEFAULT 'active', "created_at" text NOT NULL, FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "job_run" ( "id" uuid_text NOT NULL PRIMARY KEY, "job_type" varchar(100) NOT NULL, "status" varchar(20) NOT NULL, "detail" text NULL, "error" text NULL, "started_at" text NULL, "finished_at" text NULL, "created_at" text NOT NULL );
CREATE TABLE "notification" ( "id" uuid_text NOT NULL PRIMARY KEY, "user_id" uuid_text NOT NULL, "type" varchar(50) NOT NULL, "title" varchar(255) NULL, "body" text NULL, "data" text NULL, "read" boolean NOT NULL DEFAULT FALSE, "created_at" text NOT NULL, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "null_claw_exchange" ( "id" uuid_text NOT NULL PRIMARY KEY, "channel_id" uuid_text NULL, "user_message_id" uuid_text NULL, "assistant_message_id" uuid_text NULL, "prompt" text NULL, "completion" text NULL, "model" varchar(50) NULL, "latency_ms" bigint NULL, "handoff_to_human" boolean NOT NULL DEFAULT FALSE, "created_at" text NOT NULL, FOREIGN KEY ("channel_id") REFERENCES "chat_channel" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("user_message_id") REFERENCES "chat_message" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("assistant_message_id") REFERENCES "chat_message" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "payment" ( "id" uuid_text NOT NULL PRIMARY KEY, "booking_id" uuid_text NOT NULL, "user_id" uuid_text NULL, "provider" varchar(16) NOT NULL, "status" varchar(16) NOT NULL DEFAULT 'pending', "amount" bigint NOT NULL DEFAULT 0, "currency" varchar(3) NOT NULL DEFAULT 'VND', "created_at" text NOT NULL, "updated_at" text NOT NULL, "provider_txn_ref" varchar(64) NOT NULL UNIQUE, "provider_trans_id" varchar(64) NULL, "gateway_url" text NULL, "qr_payload" text NULL, "memo" text NULL, "provider_response" text NULL, "failure_reason" text NULL, "created_by" uuid_text NULL, "collected_at" text NULL, "collected_by" uuid_text NULL, FOREIGN KEY ("booking_id") REFERENCES "booking" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "permissions" ( "id" uuid_text NOT NULL PRIMARY KEY, "name" varchar(128) NOT NULL UNIQUE, "description" varchar NULL, "created_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP );
CREATE TABLE "pickup_point" ( "id" uuid_text NOT NULL PRIMARY KEY, "route_id" uuid_text NOT NULL, "name" varchar(255) NULL, "address" text NULL, "lat" double NULL, "lon" double NULL, "stop_order" integer NOT NULL DEFAULT 0, "kind" varchar(20) NULL, "created_at" text NOT NULL, FOREIGN KEY ("route_id") REFERENCES "route" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "place" ( "id" uuid_text NOT NULL PRIMARY KEY, "osm_id" bigint NOT NULL UNIQUE, "name" varchar(255) NOT NULL, "name_ascii" varchar(255) NULL, "name_no_tones" varchar(255) NULL, "type" varchar(30) NOT NULL, "province" varchar(255) NULL, "district" varchar(255) NULL, "ward" varchar(255) NULL, "lat" double NOT NULL, "lon" double NOT NULL, "population" integer NOT NULL DEFAULT 0, "created_at" text NOT NULL );
CREATE TABLE "posts" ( "id" uuid_text NOT NULL PRIMARY KEY, "author_id" uuid_text NOT NULL, "title" varchar(256) NOT NULL, "body" text NOT NULL, "created_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP, "updated_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY ("author_id") REFERENCES "user" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "price_alert" ( "id" uuid_text NOT NULL PRIMARY KEY, "phone" varchar(20) NOT NULL, "email" varchar(255) NULL, "from_name" varchar(255) NULL, "to_name" varchar(255) NULL, "route_id" uuid_text NULL, "target_price" integer NULL, "frequency" varchar(10) NOT NULL DEFAULT 'daily', "status" varchar(30) NOT NULL DEFAULT 'active', "created_at" text NOT NULL, "expires_at" text NULL, "user_id" text NULL, "last_triggered_at" text NULL, FOREIGN KEY ("route_id") REFERENCES "route" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "refresh_tokens" ( "id" uuid_text NOT NULL PRIMARY KEY, "user_id" uuid_text NOT NULL, "token_hash" varchar(128) NOT NULL, "issued_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP, "expires_at" timestamp_text NOT NULL, "revoked" boolean NOT NULL DEFAULT FALSE, "user_agent" varchar NULL, "ip" varchar NULL, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "review" ( "id" uuid_text NOT NULL PRIMARY KEY, "booking_id" uuid_text NULL, "trip_session_id" uuid_text NULL, "route_id" uuid_text NULL, "brand_id" uuid_text NULL, "author_name" varchar(255) NULL, "author_phone" varchar(20) NULL, "rating" integer NOT NULL, "title" varchar(255) NULL, "content" text NULL, "tags" text NULL, "photos" text NULL, "status" varchar(30) NOT NULL DEFAULT 'published', "helpful_count" integer NOT NULL DEFAULT 0, "reply" text NULL, "replied_at" text NULL, "created_at" text NOT NULL, "updated_at" text NOT NULL, "user_id" uuid_text NULL, FOREIGN KEY ("booking_id") REFERENCES "booking" ("id") ON DELETE SET NULL ON UPDATE CASCADE, FOREIGN KEY ("trip_session_id") REFERENCES "trip_session" ("id") ON DELETE SET NULL ON UPDATE CASCADE, FOREIGN KEY ("route_id") REFERENCES "route" ("id") ON DELETE SET NULL ON UPDATE CASCADE, FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE SET NULL ON UPDATE CASCADE, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "role_permissions" ( "role_id" uuid_text NOT NULL, "permission_id" uuid_text NOT NULL, "assigned_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY ("role_id", "permission_id"), FOREIGN KEY ("role_id") REFERENCES "roles" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("permission_id") REFERENCES "permissions" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "roles" ( "id" uuid_text NOT NULL PRIMARY KEY, "name" varchar(64) NOT NULL UNIQUE, "description" varchar NULL, "created_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP );
CREATE TABLE "route" ( "id" uuid_text NOT NULL PRIMARY KEY, "brand_id" uuid_text NULL, "name" varchar(255) NOT NULL, "start_location_id" varchar(20) NOT NULL, "end_location_id" varchar(20) NOT NULL, "status" varchar(30) NOT NULL DEFAULT 'active', "created_at" text NOT NULL, "updated_at" text NOT NULL, FOREIGN KEY ("brand_id") REFERENCES "brand" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "schedule" ( "id" uuid_text NOT NULL PRIMARY KEY, "route_id" uuid_text NOT NULL, "departure_time" text NOT NULL, "effective_from" text NULL, "effective_to" text NULL, "days_of_week" text NULL, "bus_layout_id" uuid_text NULL, "base_price_adult" bigint NOT NULL DEFAULT 0, "base_price_child" bigint NULL, "amenities" text NULL, "created_at" text NOT NULL, "vehicle_type_id" uuid_text NULL, FOREIGN KEY ("route_id") REFERENCES "route" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("bus_layout_id") REFERENCES "bus_layout" ("id") ON DELETE RESTRICT ON UPDATE CASCADE, FOREIGN KEY ("vehicle_type_id") REFERENCES "vehicle_type" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "schedule_point" ( "id" uuid_text NOT NULL PRIMARY KEY, "schedule_id" uuid_text NOT NULL, "address_id" uuid_text NOT NULL, "stop_order" integer NOT NULL DEFAULT 0, "kind" varchar(20) NULL, "created_at" text NOT NULL, "arrival_time" text NULL, FOREIGN KEY ("schedule_id") REFERENCES "schedule" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("address_id") REFERENCES "address" ("id") ON DELETE RESTRICT ON UPDATE CASCADE );
CREATE TABLE "scheduled_job" ( "id" uuid_text NOT NULL PRIMARY KEY, "job_type" varchar(100) NOT NULL, "enabled" boolean NOT NULL, "interval_days" smallint NOT NULL, "at_hour" smallint NOT NULL, "at_minute" smallint NOT NULL, "next_run_at" text NULL, "created_at" text NOT NULL, "updated_at" text NOT NULL );
CREATE TABLE IF NOT EXISTS "seaql_migrations" ( "version" varchar NOT NULL PRIMARY KEY, "applied_at" bigint NOT NULL );
CREATE TABLE "seat" ( "id" uuid_text NOT NULL PRIMARY KEY, "bus_layout_id" uuid_text NOT NULL, "seat_label" varchar(10) NOT NULL, "seat_class" varchar(30) NULL, "row_num" smallint NULL, "col_num" smallint NULL, "is_window" boolean NOT NULL DEFAULT FALSE, "floor" smallint NOT NULL DEFAULT 1, "created_at" text NOT NULL, FOREIGN KEY ("bus_layout_id") REFERENCES "bus_layout" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "seat_inventory" ( "id" uuid_text NOT NULL PRIMARY KEY, "trip_session_id" uuid_text NOT NULL, "seat_id" uuid_text NOT NULL, "status" varchar(20) NOT NULL DEFAULT 'available', "base_price" integer NOT NULL DEFAULT 0, "final_price" integer NOT NULL DEFAULT 0, "currency" varchar(3) NOT NULL DEFAULT 'VND', "held_until" text NULL, "held_by_booking_id" uuid_text NULL, "created_at" text NOT NULL, "updated_at" text NOT NULL, FOREIGN KEY ("trip_session_id") REFERENCES "trip_session" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("seat_id") REFERENCES "seat" ("id") ON DELETE RESTRICT ON UPDATE CASCADE, FOREIGN KEY ("held_by_booking_id") REFERENCES "booking" ("id") ON DELETE SET NULL ON UPDATE CASCADE );
CREATE TABLE "trip_session" ( "id" uuid_text NOT NULL PRIMARY KEY, "schedule_id" uuid_text NOT NULL, "departure_date" text NOT NULL, "actual_departure_at" text NULL, "driver_name" varchar(255) NULL, "driver_phone" varchar(20) NULL, "status" varchar(30) NOT NULL DEFAULT 'scheduled', "total_seats" bigint NOT NULL DEFAULT 0, "available_seats" bigint NOT NULL DEFAULT 0, "created_at" text NOT NULL, "updated_at" text NOT NULL, FOREIGN KEY ("schedule_id") REFERENCES "schedule" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "user" ( "id" uuid_text NOT NULL PRIMARY KEY, "brand_id" uuid_text NULL, "full_name" varchar(255) NOT NULL, "email" varchar(255) NOT NULL UNIQUE, "phone" text NULL, "email_verified_at" text NULL, "phone_verified_at" text NULL, "status" varchar(30) NOT NULL DEFAULT 'active', "block_reason" text NULL, "password_hash" varchar(255) NULL, "avatar_url" varchar(500) NULL, "locale" varchar(10) NOT NULL DEFAULT 'vi', "is_guest" boolean NOT NULL DEFAULT FALSE, "role" varchar(30) NOT NULL DEFAULT 'user', "failed_login_attempts" integer NOT NULL DEFAULT 0, "locked_until" text NULL, "last_login_at" text NULL, "last_login_ip" varchar(45) NULL, "password_changed_at" text NULL, "created_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP, "updated_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP, "oauth_provider" varchar(20) NULL, "oauth_subject" text NULL, "is_bot" boolean NOT NULL DEFAULT FALSE );
CREATE TABLE "user_roles" ( "user_id" uuid_text NOT NULL, "role_id" uuid_text NOT NULL, "assigned_at" timestamp_text NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY ("user_id", "role_id"), FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("role_id") REFERENCES "roles" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "user_verification" ( "id" uuid_text NOT NULL PRIMARY KEY, "user_id" uuid_text NOT NULL, "channel" varchar(10) NOT NULL, "target" varchar(255) NOT NULL, "code_hash" varchar(255) NOT NULL, "purpose" varchar(30) NOT NULL, "attempts" integer NOT NULL DEFAULT 0, "expires_at" text NOT NULL, "consumed_at" text NULL, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
CREATE TABLE "vehicle_type" ( "id" uuid_text NOT NULL PRIMARY KEY, "code" varchar(60) NOT NULL UNIQUE, "label" varchar(120) NOT NULL, "description" text NULL, "total_seats" smallint NULL, "sort_order" smallint NOT NULL DEFAULT 0, "status" varchar(20) NOT NULL DEFAULT 'active', "created_at" text NOT NULL, "updated_at" text NOT NULL );
CREATE TABLE "wishlist_item" ( "id" uuid_text NOT NULL PRIMARY KEY, "user_id" uuid_text NOT NULL, "route_id" uuid_text NOT NULL, "created_at" text NOT NULL, FOREIGN KEY ("user_id") REFERENCES "user" ("id") ON DELETE CASCADE ON UPDATE CASCADE, FOREIGN KEY ("route_id") REFERENCES "route" ("id") ON DELETE CASCADE ON UPDATE CASCADE );
"""

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Sheet-name token → (city id from VIETNAMESE_CITIES, display name for routes).
# Province-level ids are used for cities that are not their own province
# unit: Nha Trang → Khánh Hòa, Đà Lạt → Lâm Đồng, Sa Pa → Lào Cai,
# Huế → Thừa Thiên Huế.
CITY_BY_TOKEN = {
    "DaNang": ("da-nang", "Đà Nẵng"),
    "HaNoi": ("ha-noi", "Hà Nội"),
    "HaiPhong": ("hai-phong", "Hải Phòng"),
    "HCM": ("ho-chi-minh", "TP. Hồ Chí Minh"),
    "HaGiang": ("ha-giang", "Hà Giang"),
    "NhaTrang": ("khanh-hoa", "Nha Trang"),
    "DaLat": ("lam-dong", "Đà Lạt"),
    "SaPa": ("lao-cai", "Sa Pa"),
    "Hue": ("hue", "Huế"),
}

# All valid city ids (VIETNAMESE_CITIES) — used for validation.
VALID_CITY_IDS = {
    "ha-noi", "ho-chi-minh", "hai-phong", "da-nang", "can-tho",
    "ha-giang", "cao-bang", "bac-kan", "tuyen-quang", "lao-cai",
    "dien-bien", "lai-chau", "son-la", "yen-bai", "hoa-binh",
    "thai-nguyen", "lang-son", "quang-ninh", "bac-giang", "vinh-phuc",
    "bac-ninh", "phu-tho", "ha-nam", "thai-binh", "nam-dinh",
    "ninh-binh", "ninh-thuan", "thanh-hoa", "nghe-an", "ha-tinh",
    "quang-binh", "quang-tri", "hue", "quang-nam", "quang-ngai",
    "binh-dinh", "phu-yen", "khanh-hoa", "binh-thuan", "kon-tum",
    "gia-lai", "dak-lak", "dak-nong", "lam-dong", "ba-ria-vung-tau",
    "binh-duong", "binh-phuoc", "dong-nai", "tay-ninh", "long-an",
    "tien-giang", "ben-tre", "tra-vinh", "vinh-long", "dong-thap",
    "an-giang", "kien-giang", "hau-giang", "soc-trang", "bac-lieu",
    "ca-mau",
}

# Line format:  '<name> (<Toffset>[min])  @ <lat>, <lon>  [district]'
# The name may itself contain parentheses, so match from the right.
POINT_RE = re.compile(
    r"^(.*?)\s*\((T)([+-]?\d+)(?:min)?\)\s+@\s*"
    r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"
    r"(?:\s*\[\s*(.*?)\s*\])?\s*$"
)
TRUNC_RE = re.compile(r"^\+(\d+)\s+more\s+points")

GEOCODE_URL = "https://datxevui.com/api/places/reverse"
GEOCODE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

WARD_PREFIXES = ("phuong ", "xa ", "thi tran ")
DISTRICT_PREFIXES = ("quan ", "huyen ", "thi xa ", "thanh pho ")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str) -> str:
    """Vietnamese-aware slug: strip tones, keep [a-z0-9-]."""
    text = str(text or "").strip()
    text = text.replace("Đ", "D").replace("đ", "d")
    norm = unicodedata.normalize("NFD", text)
    ascii_ = "".join(c for c in norm if not unicodedata.combining(c))
    ascii_ = ascii_.encode("ascii", "ignore").decode("ascii")
    ascii_ = re.sub(r"[^A-Za-z0-9]+", "-", ascii_).strip("-").lower()
    return ascii_ or "brand"


def title_vi(text: str) -> str:
    """Capitalise each word (space/hyphen separated) — keeps diacritics as-is."""
    text = str(text or "").strip()
    if not text:
        return text
    def cap_word(w: str) -> str:
        return w[:1].upper() + w[1:] if w else w
    out = re.split(r"([ -])", text)
    return "".join(cap_word(p) if p not in (" ", "-") else p for p in out)


def fmt_t(mins: int) -> str:
    """T(N) time label — the user's format: T0 / T-50min / T+135min."""
    if mins == 0:
        return "T0"
    sign = "+" if mins > 0 else ""
    return f"T{sign}{mins}min"


def to_int(v, default=0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def norm_dep_time(v) -> str:
    s = str(v or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{1,2})", s)
    if not m:
        return s or "00:00"
    return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"


# --------------------------------------------------------------------------
# Excel parsing
# --------------------------------------------------------------------------
def parse_excel(path: str, max_declared: int, stats: dict) -> list[dict]:
    """Yield one record per trip-fare row across all pair sheets."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    pair_sheets = [s for s in wb.sheetnames
                   if s not in ("Summary", "Pickup & Dropoff Points")]
    records = []
    for s_i, sheet in enumerate(pair_sheets, start=1):
        try:
            from_tok, to_tok = sheet.split("_to_")
            from_city, to_city = CITY_BY_TOKEN[from_tok], CITY_BY_TOKEN[to_tok]
        except (ValueError, KeyError):
            print(f"  [warn] cannot map sheet {sheet!r} to city pair — skipped")
            continue

        ws = wb[sheet]
        header = None
        idx = None
        n_sheet_rows = 0
        for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if i == 4:
                header = row
                idx = {h: j for j, h in enumerate(header) if h}
                continue
            if i < 4 or idx is None:
                continue
            date = row[idx["Date"]]
            dep = row[idx["Dep. Time"]]
            if date is None and dep is None:
                continue
            operator = str(row[idx["Operator"]] or "").strip()
            if not operator:
                continue
            n_sheet_rows += 1

            n_pu_decl = to_int(row[idx["# Pickup"]], 0)
            n_dr_decl = to_int(row[idx["# Dropoff"]], 0)
            guard_hit = (n_pu_decl > max_declared) or (n_dr_decl > max_declared)

            rec = {
                "sheet": sheet,
                "from_city": from_city,
                "to_city": to_city,
                "date": str(date).strip()[:10],
                "dep_time": norm_dep_time(dep),
                "operator": operator,
                "vehicle": str(row[idx["Vehicle Type"]] or "").strip(),
                "orig_price": to_int(row[idx["Original (VND)"]], 0),
                "final_price": to_int(row[idx["Final (VND)"]], 0),
                "total_seats": to_int(row[idx["Total Seats"]], 0),
                "pickups": [],
                "dropoffs": [],
                "guarded": guard_hit,
            }
            if guard_hit:
                stats["guarded_rows"] += 1
            else:
                rec["pickups"] = _parse_points(row[idx["All Pickup Points"]], stats)
                rec["dropoffs"] = _parse_points(row[idx["All Dropoff Points"]], stats)
            records.append(rec)
        if s_i % 10 == 0 or s_i == len(pair_sheets):
            print(f"  parsed {s_i}/{len(pair_sheets)} sheets "
                  f"({len(records)} rows so far)")
    wb.close()
    return records


def _parse_points(cell, stats: dict) -> list[dict]:
    """Parse one multi-line 'All Pickup/Dropoff Points' cell."""
    if cell is None:
        return []
    points = []
    for line in str(cell).splitlines():
        line = line.strip()
        if not line or line in ("(no points)",):
            continue
        if TRUNC_RE.match(line):
            stats["truncated_cells"] += 1
            stats["truncated_hidden_points"] += int(TRUNC_RE.match(line).group(1))
            continue
        m = POINT_RE.match(line)
        if not m:
            stats["bad_lines"] += 1
            continue
        name, _t, mins, lat, lon, district = m.groups()
        points.append({
            "name": name.strip(),
            "t_min": int(mins),
            "t_label": fmt_t(int(mins)),
            "lat": float(lat),
            "lon": float(lon),
            "district_label": (district or "").strip(),
        })
    return points


# --------------------------------------------------------------------------
# Model building (brands / routes / layouts / addresses / schedules)
#
# Deduplication rules (v2 — "no duplicate rows in any table"):
#   brand          identity = operator name (case/space-insensitive)
#   route          identity = (brand, from-city, to-city); name is derived
#   bus_layout     identity = (brand, vehicle label, total seats)
#   address        identity = (brand, normalized point name) + 2 km geo
#                  cluster: same-name points within 2 km merge into one
#                  address whose coords are the most frequently seen
#                  pair; same-name points far apart stay distinct rows
#                  and later get a location suffix (unique name/brand)
#   schedule       identity = (route, departure time, bus layout). Excel
#                  rows of the same service (dates with slightly varying
#                  per-date stop times or point subsets) collapse into ONE
#                  schedule; its point list = the most frequently seen
#                  variant, each stop's arrival_time = the modal offset
#   schedule_point identity = (schedule, stop_order); a stop repeated with
#                  the same (address, kind) inside one schedule is dropped
# --------------------------------------------------------------------------
def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular distance in meters (fine for km-scale deltas)."""
    from math import cos, radians, sqrt
    x = radians(lon2 - lon1) * cos(radians((lat1 + lat2) / 2.0))
    y = radians(lat2 - lat1)
    return 111_320.0 * sqrt(x * x + y * y)


ADDR_MERGE_RADIUS_M = 2000.0


def build_model(records: list[dict], stats: dict) -> dict:
    brands: dict[str, dict] = {}           # norm operator name -> row
    routes: dict[tuple, dict] = {}
    layouts: dict[tuple, dict] = {}
    addresses: dict[str, dict] = {}        # address id -> row
    addr_clusters: dict[tuple, list] = {}  # (brand, norm name) -> clusters
    groups: dict[tuple, dict] = {}         # (route, dep, layout) -> group
    used_slugs: dict[str, str] = {}        # slug -> norm brand name

    def brand_of(name: str) -> dict:
        key = re.sub(r"\s+", " ", name).strip().casefold()
        b = brands.get(key)
        if b is None:
            base = slugify(name)
            slug = base
            n = 2
            while slug in used_slugs and used_slugs[slug] != key:
                slug = f"{base}-{n}"
                n += 1
            used_slugs[slug] = key
            b = {"id": str(uuid.uuid4()), "slug": slug, "name": name,
                 "status": "active", "total_trips": 0}
            brands[key] = b
        return b

    def route_of(brand: dict, from_city, to_city) -> dict:
        key = (brand["id"], from_city[0], to_city[0])
        r = routes.get(key)
        if r is None:
            r = {"id": str(uuid.uuid4()), "brand_id": brand["id"],
                 "name": f"{from_city[1]} → {to_city[1]}",
                 "start_location_id": from_city[0],
                 "end_location_id": to_city[0], "status": "active"}
            routes[key] = r
        return r

    def layout_of(brand: dict, vehicle: str, seats: int) -> dict:
        veh = re.sub(r"\s+", " ", vehicle or "").strip()
        key = (brand["id"], veh, seats if seats > 0 else 0)
        lay = layouts.get(key)
        if lay is None:
            lay = {"id": str(uuid.uuid4()), "brand_id": brand["id"],
                   "name": veh[:255] or None,
                   "vehicle_type": veh[:30] or None,
                   "total_seats": seats if seats > 0 else None}
            layouts[key] = lay
        return lay

    def address_of(brand: dict, point: dict) -> dict:
        """Return the geo-cluster this point belongs to (creating it if
        needed). Always updates the cluster's coordinate histogram so the
        address can later take its most frequently seen coordinates."""
        nkey = norm_name(point["name"]) or "(không tên)"
        clusters = addr_clusters.setdefault((brand["id"], nkey), [])
        r5 = (round(point["lat"], 5), round(point["lon"], 5))
        best = None
        best_d = None
        for c in clusters:
            d = _dist_m(point["lat"], point["lon"], c["lat"], c["lon"])
            if d <= ADDR_MERGE_RADIUS_M and (best_d is None or d < best_d):
                best, best_d = c, d
        if best is None:
            display = (point["name"].strip() or "(không tên)")[:255]
            a = {"id": str(uuid.uuid4()), "brand_id": brand["id"],
                 "name": display, "lat": point["lat"], "lon": point["lon"],
                 "district_label": point["district_label"],
                 "geocoded": False, "province": None, "district": None,
                 "ward": None}
            best = {"addr": a, "lat": point["lat"], "lon": point["lon"],
                    "coord_counts": {}, "first_exact": {}}
            clusters.append(best)
            addresses[a["id"]] = a
        best["coord_counts"][r5] = best["coord_counts"].get(r5, 0) + 1
        best["first_exact"].setdefault(r5, (point["lat"], point["lon"]))
        return best

    for rec in records:
        brand = brand_of(rec["operator"])
        route = route_of(brand, rec["from_city"], rec["to_city"])
        layout = layout_of(brand, rec["vehicle"], rec["total_seats"])

        # One schedule per (route, departure time, bus layout) — the true
        # recurring-service identity. Excel rows that describe the same
        # service (same departure, different dates, slightly varying stop
        # times or point subsets) all merge into this single group.
        gkey = (route["id"], rec["dep_time"], layout["id"])
        g = groups.get(gkey)
        if g is None:
            g = {"id": str(uuid.uuid4()), "route_id": route["id"],
                 "brand_id": brand["id"],
                 "departure_time": rec["dep_time"],
                 "bus_layout_id": layout["id"],
                 "dates": set(), "prices": [], "variants": {}}
            groups[gkey] = g
        g["dates"].add(rec["date"])
        if rec["orig_price"] > 0:
            g["prices"].append(rec["orig_price"])
        elif rec["final_price"] > 0:
            g["prices"].append(rec["final_price"])

        if rec["guarded"]:
            # aggregation-bug row: dates/prices are valid, points are not
            v = g["variants"].setdefault((), {"count": 0, "times": [],
                                              "n_points": 0})
            v["count"] += 1
            continue

        # resolve every point to its address cluster (also feeds the
        # coordinate histogram), then key the row's point-list variant by
        # the sequence of (segment, cluster) — WITHOUT times, so per-date
        # time jitter does not split the service.
        seg_points = ([("pu", p) for p in rec["pickups"]] +
                      [("do", p) for p in rec["dropoffs"]])
        clusters = [address_of(brand, p) for _, p in seg_points]
        vid = tuple((seg, c["addr"]["id"])
                    for (seg, _), c in zip(seg_points, clusters))
        v = g["variants"].get(vid)
        if v is None:
            v = {"count": 0, "times": [Counter() for _ in vid],
                 "n_points": len(vid)}
            g["variants"][vid] = v
        v["count"] += 1
        for (seg, p), cnt in zip(seg_points, v["times"]):
            cnt[p["t_min"]] += 1

    # ---- finalize addresses: coords = most frequently seen pair ---------
    for clist in addr_clusters.values():
        for c in clist:
            r5 = max(c["coord_counts"].items(), key=lambda kv: kv[1])[0]
            c["addr"]["lat"], c["addr"]["lon"] = c["first_exact"][r5]

    # ---- finalize schedules: canonical variant + modal stop times -------
    schedules: dict[tuple, dict] = {}
    merged_variants = 0
    dup_stop_dropped = 0
    for gkey, g in groups.items():
        merged_variants += len(g["variants"]) - 1
        # canonical variant: prefer one that actually has points, then the
        # most frequently seen, then the most complete (most stops)
        best_vid = best = best_rank = None
        for vid, v in g["variants"].items():
            rank = (v["n_points"] > 0, v["count"], v["n_points"])
            if best is None or rank > best_rank:
                best_vid, best, best_rank = vid, v, rank
        dates = sorted(d for d in g["dates"]
                       if re.match(r"^\d{4}-\d{2}-\d{2}$", d))
        wds = sorted({datetime.strptime(d, "%Y-%m-%d").isoweekday()
                      for d in dates})
        s = {"id": g["id"], "route_id": g["route_id"],
             "brand_id": g["brand_id"],
             "departure_time": g["departure_time"],
             "bus_layout_id": g["bus_layout_id"],
             "effective_from": dates[0] if dates else None,
             "effective_to": dates[-1] if dates else None,
             "days_of_week": ",".join(str(w) for w in wds),
             "base_price_adult": min(g["prices"]) if g["prices"] else 0,
             "points": []}
        seen = set()
        for i, (seg, addr_id) in enumerate(best_vid):
            t_mode = best["times"][i].most_common(1)[0][0]
            kind = ("drop" if seg == "do"
                    else ("pickup" if t_mode <= 0 else "middle"))
            if (addr_id, kind) in seen:
                dup_stop_dropped += 1        # same stop twice — keep first
                continue
            seen.add((addr_id, kind))
            s["points"].append({"address_id": addr_id,
                                "stop_order": len(s["points"]) + 1,
                                "kind": kind,
                                "arrival_time": fmt_t(t_mode)})
        schedules[gkey] = s

    # brand trip counts
    per_brand = defaultdict(int)
    for s in schedules.values():
        per_brand[s["brand_id"]] += 1
    for b in brands.values():
        b["total_trips"] = per_brand.get(b["id"], 0)

    stats.update({
        "n_records": len(records),
        "n_brands": len(brands),
        "n_routes": len(routes),
        "n_layouts": len(layouts),
        "n_addresses": len(addresses),
        "n_schedules": len(schedules),
        "n_service_variants_merged": merged_variants,
        "n_dup_stops_dropped": dup_stop_dropped,
    })
    return {"brands": brands, "routes": routes, "layouts": layouts,
            "addresses": addresses, "schedules": schedules}

# --------------------------------------------------------------------------
# Reverse geocoding (datxevui.com API, cached + threaded)
# --------------------------------------------------------------------------
class Geocoder:
    def __init__(self, cache_path: str, workers: int = 6):
        self.cache_path = Path(cache_path)
        self.cache: dict[str, dict | None] = {}
        if self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text())
            except (json.JSONDecodeError, OSError):
                print(f"  [warn] geocode cache unreadable, starting fresh")
        self.workers = workers
        self._local = threading.local()
        self._lock = threading.Lock()
        self.stats = {"api_calls": 0, "ok": 0, "empty": 0, "failed": 0,
                      "cache_hits": 0}

    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": GEOCODE_UA,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://datxevui.com/",
            })
            self._local.session = sess
        return sess

    def _fetch_one(self, lat: float, lon: float) -> dict | None:
        """Query the API for one coordinate. Returns record dict, or
        None for empty result / permanent failure (not cached on failure)."""
        url = f"{GEOCODE_URL}"
        for attempt in range(4):
            try:
                r = self._session().get(
                    url, params={"lat": f"{lat}", "lon": f"{lon}", "limit": 1},
                    timeout=12)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    return None            # empty result — cache it
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1.0 * (attempt + 1))
                    continue
                # 403/other — treat as permanent failure for this run
                print(f"  [geocode] HTTP {r.status_code} for {lat},{lon} "
                      f"— giving up on this coordinate")
                return {"__failed__": True}
            except (requests.RequestException, ValueError):
                time.sleep(1.0 * (attempt + 1))
        return {"__failed__": True}

    def lookup(self, lat: float, lon: float) -> dict | None:
        key = f"{lat:.5f},{lon:.5f}"
        if key in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[key]
        rec = self._fetch_one(lat, lon)
        with self._lock:
            self.stats["api_calls"] += 1
            if rec is None:
                self.stats["empty"] += 1
                self.cache[key] = None
            elif isinstance(rec, dict) and rec.get("__failed__"):
                self.stats["failed"] += 1
                return None              # do NOT cache hard failures
            else:
                self.stats["ok"] += 1
                self.cache[key] = rec
        return rec

    def save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False))


def geocode_addresses(model: dict, cache_path: str, workers: int, stats: dict,
                      use_api: bool = True):
    """Fill province/district/ward on every address (one API call per
    unique non-zero coordinate)."""
    addresses = model["addresses"]
    coords = sorted({(a["lat"], a["lon"]) for a in addresses.values()
                     if a["lat"] != 0.0 or a["lon"] != 0.0})
    geo = Geocoder(cache_path, workers)
    print(f"  unique coordinates: {len(coords)} "
          f"(cache: {len(geo.cache)} entries)")
    if coords and use_api:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(geo.lookup, lat, lon): (lat, lon)
                       for lat, lon in coords}
            done = 0
            for fut in as_completed(futures):
                fut.result()
                done += 1
                if done % 250 == 0:
                    print(f"    geocoded {done}/{len(coords)}")
    elif coords:
        print("      offline mode: applying cache / label fallback only")
    geo.save()
    stats["geocode"] = dict(geo.stats)

    # apply results to addresses (fallback: VeXeRe district label)
    for a in addresses.values():
        if a["lat"] == 0.0 and a["lon"] == 0.0:
            a["district"] = title_vi(a["district_label"]) or None
            continue
        rec = geo.cache.get(f"{a['lat']:.5f},{a['lon']:.5f}")
        if rec:
            a["province"] = title_vi(rec.get("province")) or None
            dv = str(rec.get("district") or "").strip().lower()
            raw = str(rec.get("district") or "").strip()
            if dv.startswith(WARD_PREFIXES):
                a["ward"] = title_vi(raw)
            elif dv:
                a["district"] = title_vi(raw)
            a["geocoded"] = True
        else:
            a["district"] = title_vi(a["district_label"]) or None


def disambiguate_address_names(model: dict) -> int:
    """Make address names unique per brand. When one normalized name maps
    to several geo-clusters (genuinely far-apart points sharing a name),
    every row after the first gets a location-hint suffix so that no two
    rows in the address table look like duplicates of each other."""
    grouped: dict[tuple, list] = defaultdict(list)
    for a in model["addresses"].values():
        grouped[(a["brand_id"], norm_name(a["name"]))].append(a)
    renamed = 0
    for lst in grouped.values():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda a: (a["lat"], a["lon"]))
        for k, a in enumerate(lst[1:], start=2):
            base = a["name"].strip()
            hint = a.get("province") or a.get("district") or a.get("ward")
            suffix = f" ({hint})" if hint else f" ({k})"
            new = base + suffix
            if len(new) > 255:
                new = base[:255 - len(suffix)] + suffix
            a["name"] = new
            renamed += 1

    # final safety pass: number any residual same-name rows
    grouped2: dict[tuple, list] = defaultdict(list)
    for a in model["addresses"].values():
        grouped2[(a["brand_id"], norm_name(a["name"]))].append(a)
    for lst in grouped2.values():
        if len(lst) < 2:
            continue
        for k, a in enumerate(lst[1:], start=2):
            base = a["name"].strip()
            suffix = f" #{k}"
            new = base + suffix
            if len(new) > 255:
                new = base[:255 - len(suffix)] + suffix
            a["name"] = new
            renamed += 1
    return renamed


# --------------------------------------------------------------------------
# SQLite writing
# --------------------------------------------------------------------------
def write_db(db_path: str, model: dict):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA_SQL)
    # ---- duplicate guards: created BEFORE the inserts so any logical
    # duplicate (not just exact row copy) fails loudly at INSERT time.
    con.executescript(
        'CREATE UNIQUE INDEX "ux_route_ident" ON "route"'
        ' ("brand_id", "name");\n'
        'CREATE UNIQUE INDEX "ux_bus_layout_ident" ON "bus_layout"'
        ' ("brand_id", ifnull("name", \'\'),'
        ' ifnull("total_seats", -1));\n'
        'CREATE UNIQUE INDEX "ux_address_ident" ON "address"'
        ' ("brand_id", "name");\n'
        'CREATE UNIQUE INDEX "ux_schedule_service" ON "schedule"'
        ' ("route_id", "departure_time", ifnull("bus_layout_id", \'\'));\n'
        'CREATE UNIQUE INDEX "ux_schedule_point_stop" ON "schedule_point"'
        ' ("schedule_id", "stop_order");')
    ts = now_iso()

    con.execute("BEGIN")
    con.executemany(
        "INSERT INTO brand (id, slug, name, logo_url, description,"
        " contact_phone, contact_email, rating, status, accent_color,"
        " total_trips, created_at, updated_at)"
        " VALUES (?,?,?,NULL,NULL,NULL,NULL,NULL,?,NULL,?,?,?)",
        [(b["id"], b["slug"], b["name"], b["status"], b["total_trips"], ts, ts)
         for b in model["brands"].values()])

    con.executemany(
        "INSERT INTO route (id, brand_id, name, start_location_id,"
        " end_location_id, status, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(r["id"], r["brand_id"], r["name"], r["start_location_id"],
          r["end_location_id"], r["status"], ts, ts)
         for r in model["routes"].values()])

    con.executemany(
        "INSERT INTO bus_layout (id, brand_id, name, vehicle_type,"
        " total_seats, layout_data, created_at, updated_at)"
        " VALUES (?,?,?,?,?,NULL,?,?)",
        [(l["id"], l["brand_id"], l["name"], l["vehicle_type"],
          l["total_seats"], ts, ts)
         for l in model["layouts"].values()])

    con.executemany(
        "INSERT INTO address (id, brand_id, name, address, lat, lon,"
        " province, district, ward, created_at, updated_at)"
        " VALUES (?,?,?,NULL,?,?,?,?,?,?,?)",
        [(a["id"], a["brand_id"], a["name"], a["lat"], a["lon"],
          a["province"], a["district"], a["ward"], ts, ts)
         for a in model["addresses"].values()])

    con.executemany(
        "INSERT INTO schedule (id, route_id, departure_time, effective_from,"
        " effective_to, days_of_week, bus_layout_id, base_price_adult,"
        " base_price_child, amenities, created_at, vehicle_type_id)"
        " VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,NULL)",
        [(s["id"], s["route_id"], s["departure_time"], s["effective_from"],
          s["effective_to"], s["days_of_week"], s["bus_layout_id"],
          s["base_price_adult"], ts)
         for s in model["schedules"].values()])

    sp_rows = []
    for s in model["schedules"].values():
        for p in s["points"]:
            sp_rows.append((str(uuid.uuid4()), s["id"], p["address_id"],
                            p["stop_order"], p["kind"], ts, p["arrival_time"]))
    con.executemany(
        "INSERT INTO schedule_point (id, schedule_id, address_id, stop_order,"
        " kind, created_at, arrival_time) VALUES (?,?,?,?,?,?,?)",
        sp_rows)
    con.commit()
    con.close()
    return len(sp_rows)


# --------------------------------------------------------------------------
# Verification report
# --------------------------------------------------------------------------
def verify_db(db_path: str):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    print("\n" + "=" * 72)
    print("VERIFICATION REPORT — " + db_path)
    print("=" * 72)

    counts = {}
    for t in ("brand", "route", "bus_layout", "address", "schedule",
              "schedule_point"):
        counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print("Row counts:", json.dumps(counts, indent=2))

    # FK integrity
    print("\nFK integrity:")
    checks = [
        ("schedule → route", "SELECT COUNT(*) FROM schedule s LEFT JOIN route r"
         " ON s.route_id = r.id WHERE r.id IS NULL"),
        ("schedule → bus_layout", "SELECT COUNT(*) FROM schedule s LEFT JOIN"
         " bus_layout b ON s.bus_layout_id = b.id WHERE b.id IS NULL"),
        ("schedule_point → schedule", "SELECT COUNT(*) FROM schedule_point p"
         " LEFT JOIN schedule s ON p.schedule_id = s.id WHERE s.id IS NULL"),
        ("schedule_point → address", "SELECT COUNT(*) FROM schedule_point p"
         " LEFT JOIN address a ON p.address_id = a.id WHERE a.id IS NULL"),
        ("address → brand", "SELECT COUNT(*) FROM address a LEFT JOIN brand b"
         " ON a.brand_id = b.id WHERE b.id IS NULL"),
        ("route → brand", "SELECT COUNT(*) FROM route r LEFT JOIN brand b"
         " ON r.brand_id = b.id WHERE b.id IS NULL"),
    ]
    for name, sql in checks:
        n = con.execute(sql).fetchone()[0]
        print(f"  {name}: {n} orphans")

    # route city ids valid
    bad = con.execute(
        "SELECT COUNT(*) FROM route WHERE start_location_id NOT IN (%s)"
        " OR end_location_id NOT IN (%s)" %
        (",".join(f"'{c}'" for c in VALID_CITY_IDS),
         ",".join(f"'{c}'" for c in VALID_CITY_IDS))).fetchone()[0]
    print(f"\nroute city ids invalid: {bad}")
    top_routes = con.execute(
        "SELECT name, start_location_id, end_location_id, COUNT(*) n FROM"
        " route GROUP BY start_location_id, end_location_id"
        " ORDER BY n DESC LIMIT 8").fetchall()
    print("Top route pairs:", [(r["name"], r["n"]) for r in top_routes])

    # kind distribution + T format check
    kinds = con.execute(
        "SELECT kind, COUNT(*) n FROM schedule_point GROUP BY kind").fetchall()
    print("\nschedule_point kinds:", {r["kind"]: r["n"] for r in kinds})
    bad_t = con.execute(
        "SELECT COUNT(*) FROM schedule_point WHERE arrival_time IS NOT NULL"
        " AND arrival_time NOT GLOB 'T*min' AND arrival_time != 'T0'").fetchone()[0]
    print(f"arrival_time values not matching T(N) format: {bad_t}")

    # ---- duplicate audit: every seeded table must be duplicate-free ----
    print("\nDuplicate audit (every count must be 0):")
    dup_checks = [
        ("brand      exact business rows", """
            SELECT COUNT(*) FROM (SELECT slug, name, logo_url, description,
              contact_phone, contact_email, rating, status, accent_color,
              total_trips, COUNT(*) c FROM brand GROUP BY slug, name,
              logo_url, description, contact_phone, contact_email, rating,
              status, accent_color, total_trips HAVING c > 1)"""),
        ("route      exact business rows", """
            SELECT COUNT(*) FROM (SELECT brand_id, name, start_location_id,
              end_location_id, status, COUNT(*) c FROM route GROUP BY
              brand_id, name, start_location_id, end_location_id, status
              HAVING c > 1)"""),
        ("bus_layout exact business rows", """
            SELECT COUNT(*) FROM (SELECT brand_id, name, vehicle_type,
              total_seats, layout_data, COUNT(*) c FROM bus_layout GROUP BY
              brand_id, name, vehicle_type, total_seats, layout_data
              HAVING c > 1)"""),
        ("address    exact business rows", """
            SELECT COUNT(*) FROM (SELECT brand_id, name, address, lat, lon,
              province, district, ward, COUNT(*) c FROM address GROUP BY
              brand_id, name, address, lat, lon, province, district, ward
              HAVING c > 1)"""),
        ("schedule   exact business rows", """
            SELECT COUNT(*) FROM (SELECT route_id, departure_time,
              effective_from, effective_to, days_of_week, bus_layout_id,
              base_price_adult, base_price_child, amenities, vehicle_type_id,
              COUNT(*) c FROM schedule GROUP BY route_id, departure_time,
              effective_from, effective_to, days_of_week, bus_layout_id,
              base_price_adult, base_price_child, amenities, vehicle_type_id
              HAVING c > 1)"""),
        ("schedule_point exact business rows", """
            SELECT COUNT(*) FROM (SELECT schedule_id, address_id, stop_order,
              kind, arrival_time, COUNT(*) c FROM schedule_point GROUP BY
              schedule_id, address_id, stop_order, kind, arrival_time
              HAVING c > 1)"""),
        ("brand      same name, different slug", """
            SELECT COUNT(*) FROM (SELECT LOWER(TRIM(name)) n, COUNT(*) c
              FROM brand GROUP BY LOWER(TRIM(name)) HAVING c > 1)"""),
        ("address    same brand + same name (any case)", """
            SELECT COUNT(*) FROM (SELECT brand_id, LOWER(TRIM(name)),
              COUNT(*) c FROM address GROUP BY brand_id, LOWER(TRIM(name))
              HAVING c > 1)"""),
        ("schedule   same route + dep + layout", """
            SELECT COUNT(*) FROM (SELECT route_id, departure_time,
              ifnull(bus_layout_id, ''), COUNT(*) c FROM schedule GROUP BY
              route_id, departure_time, ifnull(bus_layout_id, '')
              HAVING c > 1)"""),
        ("schedule_point same schedule + address + kind", """
            SELECT COUNT(*) FROM (SELECT schedule_id, address_id, kind,
              COUNT(*) c FROM schedule_point GROUP BY schedule_id,
              address_id, kind HAVING c > 1)"""),
    ]
    failures = 0
    for label, sql in dup_checks:
        n = con.execute(sql).fetchone()[0]
        mark = "ok" if n == 0 else "DUPLICATES!"
        if n:
            failures += 1
        print(f"  {label}: {n}  [{mark}]")

    # informational: distinct addresses sharing the same coordinates —
    # different named service points at one location (e.g. \u0110\u00f3n/
    # Tr\u1ea3 labels) are intentionally kept separate; the pickup/drop
    # role already lives in schedule_point.kind
    n_shared = con.execute("""
        SELECT COUNT(*) FROM (SELECT a.brand_id, round(a.lat, 5), round(a.lon, 5),
          COUNT(*) c FROM address a GROUP BY a.brand_id, round(a.lat, 5),
          round(a.lon, 5) HAVING c > 1)""").fetchone()[0]
    print(f"  (info) distinct named addresses sharing coordinates: {n_shared}")
    n_empty = con.execute(
        "SELECT COUNT(*) FROM schedule WHERE bus_layout_id IS NULL").fetchone()[0]
    print(f"  (info) schedules without layout: {n_empty}")

    # geocode coverage
    cov = con.execute(
        "SELECT COUNT(*) total, SUM(province IS NOT NULL) with_prov,"
        " SUM(district IS NOT NULL) with_dist, SUM(ward IS NOT NULL) with_ward,"
        " SUM(lat = 0 AND lon = 0) zero_coord FROM address").fetchone()
    print(f"\nAddress admin coverage: total={cov['total']}, "
          f"province={cov['with_prov']}, district={cov['with_dist']}, "
          f"ward={cov['with_ward']}, zero-coord={cov['zero_coord']}")

    print("\nSample geocoded addresses:")
    for r in con.execute(
            "SELECT a.name, a.lat, a.lon, a.province, a.district, a.ward,"
            " b.name brand FROM address a JOIN brand b ON a.brand_id = b.id"
            " WHERE a.province IS NOT NULL AND a.ward IS NOT NULL LIMIT 6"):
        print(f"  [{r['brand']}] {r['name']} @({r['lat']:.5f},{r['lon']:.5f})"
              f" → {r['province']}/{r['district'] or '-'}/{r['ward']}")

    print("\nSample schedule with points (first with 3+ points):")
    sched = con.execute(
        "SELECT s.id, b.name brand, r.name route, s.departure_time,"
        " s.days_of_week, s.effective_from, s.effective_to,"
        " s.base_price_adult, l.vehicle_type, COUNT(p.id) npts"
        " FROM schedule s JOIN route r ON s.route_id = r.id"
        " JOIN brand b ON r.brand_id = b.id"
        " LEFT JOIN bus_layout l ON s.bus_layout_id = l.id"
        " LEFT JOIN schedule_point p ON p.schedule_id = s.id"
        " GROUP BY s.id HAVING npts >= 5 ORDER BY b.total_trips DESC LIMIT 1"
    ).fetchone()
    if sched:
        print(f"  {sched['brand']} | {sched['route']} | dep {sched['departure_time']}"
              f" | {sched['vehicle_type']} | {sched['base_price_adult']:,} VND"
              f" | days {sched['days_of_week']} | eff"
              f" {sched['effective_from']}..{sched['effective_to']}"
              f" | {sched['npts']} points")
        for p in con.execute(
                "SELECT p.stop_order, p.kind, p.arrival_time, a.name,"
                " a.province, a.ward, a.district FROM schedule_point p"
                " JOIN address a ON p.address_id = a.id WHERE p.schedule_id = ?"
                " ORDER BY p.stop_order", (sched["id"],)):
            loc = p["ward"] or p["district"] or ""
            prov = p["province"] or ""
            print(f"    #{p['stop_order']:<2} {p['kind']:<7}"
                  f" {p['arrival_time']:<9} {p['name'][:44]:<46}"
                  f" {loc[:18]:<18} {prov[:22]}")

    # top brands
    print("\nTop brands by schedules:")
    for r in con.execute("SELECT name, slug, total_trips FROM brand"
                         " ORDER BY total_trips DESC LIMIT 8"):
        print(f"  {r['name']:<28} {r['slug']:<28} {r['total_trips']} schedules")
    con.close()
    return failures


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--excel", default="/home/z/my-project/download/"
                    "vexere_nxn_trips_enriched.xlsx")
    ap.add_argument("--db", default="/home/z/my-project/download/"
                    "booking_seed.sqlite")
    ap.add_argument("--cache", default="/home/z/my-project/download/"
                    "reverse_geocode_cache.json")
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrent reverse-geocode requests (default 6)")
    ap.add_argument("--max-declared-points", type=int, default=30,
                    help="rows whose declared # Pickup/# Dropoff exceeds this"
                         " are treated as the aggregation-bug rows and their"
                         " point lists are skipped (default 30)")
    ap.add_argument("--no-geocode", action="store_true",
                    help="skip the reverse-geocode API (uses cache + labels)")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the parsed-records cache and re-parse Excel")
    args = ap.parse_args()

    stats = defaultdict(int)
    stats["guarded_rows"] = 0
    stats["truncated_cells"] = 0
    stats["truncated_hidden_points"] = 0
    stats["bad_lines"] = 0

    # parsed-records cache (protects long runs from timeouts)
    rec_cache = args.db + ".records.json.gz"
    excel_mtime = os.path.getmtime(args.excel) if os.path.exists(args.excel) else 0
    cached_meta = None
    records = None
    if not args.fresh and os.path.exists(rec_cache):
        try:
            with gzip.open(rec_cache, "rt", encoding="utf-8") as f:
                blob = json.load(f)
            cached_meta = blob.get("meta", {})
            if (cached_meta.get("excel_mtime") == excel_mtime
                    and cached_meta.get("max_declared") == args.max_declared_points):
                records = blob["records"]
                stats.update(blob.get("stats", {}))
                print(f"[1/4] Loaded {len(records)} records from cache "
                      f"({rec_cache})")
            else:
                print("[1/4] Records cache stale (excel or guard changed) —"
                      " re-parsing")
        except (OSError, json.JSONDecodeError, ValueError):
            print("[1/4] Records cache unreadable — re-parsing")

    if records is None:
        print(f"[1/4] Parsing Excel: {args.excel}")
        t0 = time.time()
        records = parse_excel(args.excel, args.max_declared_points, stats)
        print(f"      {len(records)} rows parsed in {time.time() - t0:.1f}s")
        try:
            with gzip.open(rec_cache, "wt", encoding="utf-8") as f:
                json.dump({"meta": {"excel_mtime": excel_mtime,
                                   "max_declared": args.max_declared_points},
                           "stats": dict(stats),
                           "records": records}, f, ensure_ascii=False)
        except OSError:
            pass

    print("[2/4] Building model (brands / routes / layouts / addresses /"
          " schedules)")
    model = build_model(records, stats)
    n_points = sum(len(s["points"]) for s in model["schedules"].values())
    print(f"      {stats['n_brands']} brands, {stats['n_routes']} routes,"
          f" {stats['n_layouts']} layouts, {stats['n_addresses']} addresses,"
          f" {stats['n_schedules']} schedules, {n_points} schedule points")
    print(f"      merged {stats['n_service_variants_merged']} duplicate"
          f" service variants; dropped {stats['n_dup_stops_dropped']}"
          f" repeated stops")

    print("[3/4] Reverse geocoding unique coordinates")
    t0 = time.time()
    geocode_addresses(model, args.cache, args.workers, stats,
                      use_api=not args.no_geocode)
    gs = stats["geocode"]
    print(f"      API calls: {gs['api_calls']}, ok: {gs['ok']},"
          f" empty: {gs['empty']}, failed: {gs['failed']},"
          f" cache hits: {gs['cache_hits']} ({time.time() - t0:.1f}s)")

    n_renamed = disambiguate_address_names(model)
    print(f"      address names disambiguated (location suffix):"
          f" {n_renamed}")

    print(f"[4/4] Writing SQLite: {args.db}")
    n_sp = write_db(args.db, model)
    print(f"      wrote {n_sp} schedule_point rows")

    print("\nParse stats: guarded rows (bug suspect, points skipped): "
          f"{stats['guarded_rows']}; truncated cells: "
          f"{stats['truncated_cells']} (hidden points not in Excel: "
          f"{stats['truncated_hidden_points']}); unparsable lines: "
          f"{stats['bad_lines']}")
    failures = verify_db(args.db)
    if failures:
        raise SystemExit(
            f"\nFAILED: {failures} duplicate check(s) did not pass — "
            "see the audit above")
    print("\nAll duplicate checks passed.")
    print("Done.")


if __name__ == "__main__":
    main()
