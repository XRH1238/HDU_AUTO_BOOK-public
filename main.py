import json
import logging
import os
import random
import time
from datetime import datetime, timedelta

import requests
import yaml
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait


logging.basicConfig(
    format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    level=logging.DEBUG,
)

TIME_ZONE = 8
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def get_target_weekday(offset_days=2):
    return WEEKDAY_NAMES[(datetime.now().weekday() + offset_days) % 7]


def get_seats_with_config(user_config, date_config, seat_config):
    seat_name = date_config["name"]
    if seat_name == "自定义":
        return user_config["自定义"]
    return list(range(seat_config[seat_name]["begin"], seat_config[seat_name]["end"]))


class SeatAutoBooker:
    def __init__(self, booker_config):
        self.json = None
        self.resp = None
        self.user_data = None
        self.cookie_items = []

        logging.info("Creating SeatAutoBooker object")

        self.un = os.environ["SCHOOL_ID"].strip()
        print("使用用户：{}".format(self.un))
        self.pd = os.environ["PASSWORD"].strip()
        self.SCKey = os.environ.get("SCKEY", "")
        if not self.SCKey:
            print("没有Server酱的key, 将不会推送消息")

        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        self.driver = webdriver.Chrome(
            service=Service("/usr/local/bin/chromedriver"),
            options=chrome_options,
        )
        self.wait = WebDriverWait(self.driver, 20, 0.5)
        self.cookie = None
        self.cfg = booker_config

    def _refresh_cookie_header_from_items(self):
        if not self.cookie_items:
            self.cookie = ""
            return
        self.cookie = ";".join(
            ["{}={}".format(item["name"], item["value"]) for item in self.cookie_items]
        )

    def _build_requests_session(self):
        session = requests.Session()
        for key, value in self.cfg["headers"].items():
            if key.lower() != "cookie":
                session.headers[key] = value
        for item in self.cookie_items:
            session.cookies.set(
                item["name"],
                item["value"],
                domain=item.get("domain"),
                path=item.get("path", "/"),
            )
        return session

    def _sync_cookie_items_from_session(self, session):
        self.cookie_items = []
        for cookie in session.cookies:
            self.cookie_items.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or "",
                    "path": cookie.path or "/",
                }
            )
        self._refresh_cookie_header_from_items()

    def _dump_login_debug(self):
        try:
            current_url = self.driver.current_url
        except Exception:
            current_url = "<unavailable>"
        try:
            page_title = self.driver.title
        except Exception:
            page_title = "<unavailable>"

        logging.error("Login debug current_url=%s", current_url)
        logging.error("Login debug title=%s", page_title)

        try:
            with open("login_debug.html", "w", encoding="utf-8") as f_obj:
                f_obj.write(self.driver.page_source)
            logging.info("Saved login debug HTML to login_debug.html")
        except Exception:
            logging.exception("Failed to save login_debug.html")

        try:
            self.driver.save_screenshot("login_debug.png")
            logging.info("Saved login debug screenshot to login_debug.png")
        except Exception:
            logging.exception("Failed to save login_debug.png")

    def _dump_user_info_debug(self, response):
        try:
            with open("user_info_debug.txt", "w", encoding="utf-8") as f_obj:
                f_obj.write("status_code={}\n".format(response.status_code))
                f_obj.write("url={}\n\n".format(response.url))
                f_obj.write(response.text)
            logging.info("Saved user info debug response to user_info_debug.txt")
        except Exception:
            logging.exception("Failed to save user_info_debug.txt")

    def _set_input_value(self, element, value):
        element.click()
        element.clear()
        element.send_keys(value)
        current_value = element.get_attribute("value") or ""
        if current_value == value:
            return

        logging.warning("send_keys did not persist, falling back to JS value setter")
        self.driver.execute_script(
            """
            const element = arguments[0];
            const value = arguments[1];
            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype,
                'value'
            ).set;
            setter.call(element, value);
            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
            element.dispatchEvent(new Event('blur', { bubbles: true }));
            """,
            element,
            value,
        )

    def book_favorite_seat(self, user_config, seat_config):
        target_weekday = get_target_weekday(offset_days=2)
        seat_type = seat_config[user_config[target_weekday]["name"]]["type"]
        if seat_type == "自习室":
            start_time = datetime.now().replace(
                hour=20 - TIME_ZONE, minute=0, second=0, microsecond=0
            )
            end_time = datetime.now().replace(
                hour=20 - TIME_ZONE, minute=15, second=0, microsecond=0
            )
        else:
            start_time = datetime.now().replace(
                hour=21 - TIME_ZONE, minute=0, second=0, microsecond=0
            )
            end_time = datetime.now().replace(
                hour=21 - TIME_ZONE, minute=15, second=0, microsecond=0
            )

        start_time = start_time - timedelta(minutes=self.cfg["cron-delta-minutes"])
        if datetime.now() < start_time or datetime.now() > end_time:
            return -1, "未到预约时间"

        logging.info("Booking favorite seat")
        retry_sleep_time = (
            timedelta(minutes=self.cfg["cron-delta-minutes"]).seconds * 2
            / (self.cfg["max-retry"] - 2)
            - 10
        )
        for tried_times in range(self.cfg["max-retry"]):
            try:
                return self._book_favorite_seat(user_config, seat_config, tried_times)
            except Exception as exc:
                logging.exception(exc)
                print(exc.__class__, "尝试第{}次".format(tried_times))
                time.sleep(retry_sleep_time)

    def _book_favorite_seat(self, user_config, seat_config, tried_times=0):
        logging.info("Entering _book_favorite_seat method")
        target_weekday = get_target_weekday(offset_days=2)
        date_config = user_config[target_weekday]
        seats = get_seats_with_config(user_config, date_config, seat_config)
        today_0_clock = datetime.strptime(
            datetime.now().strftime("%Y-%m-%d 00:00:00"), "%Y-%m-%d %H:%M:%S"
        )
        book_time = today_0_clock + timedelta(days=2) + timedelta(
            hours=date_config["开始时间"]
        )
        delta = book_time - self.cfg["start-time"]
        total_seconds = delta.days * 24 * 3600 + delta.seconds

        if date_config["name"] == "自定义" and tried_times < self.cfg["max-retry"] / 3 * 2:
            seat = seats[0]
        else:
            seat = random.choice(seats)

        data = (
            f"beginTime={total_seconds}&duration={3600 * date_config['持续小时数']}"
            f"&&seats[0]={seat}&seatBookers[0]={self.user_data['uid']}"
        )

        headers = self.cfg["headers"]
        headers["Cookie"] = self.cookie
        print(data)
        self.resp = requests.post(self.cfg["target"], data=data, headers=headers)
        self.json = json.loads(self.resp.text)
        return self.json["CODE"], self.json["MESSAGE"] + " 座位:{}".format(seat)

    def login(self):
        logging.info("Login in")

        username_selector = (
            By.XPATH,
            "//form[@action='login']//input[@name='username' and not(@type='hidden')]",
        )
        password_selector = (
            By.XPATH,
            "//form[@action='login']//input[@type='password']",
        )
        button_selector = (
            By.XPATH,
            "//form[@action='login']//button[@type='submit']",
        )

        try:
            logging.info("开始登录...")
            self.driver.get("https://hdu.huitu.zhishulib.com/")
            logging.debug("打开网站")

            username_input = self.wait.until(
                EC.visibility_of_element_located(username_selector)
            )
            logging.debug("找到用户名输入框")

            password_input = self.wait.until(
                EC.visibility_of_element_located(password_selector)
            )
            logging.debug("找到密码输入框")

            login_button = self.wait.until(
                EC.element_to_be_clickable(button_selector)
            )
            logging.debug("找到登录按钮")

            self._set_input_value(username_input, self.un)
            logging.info("输入用户名")
            logging.debug("Username field value length=%s", len(username_input.get_attribute("value") or ""))

            self._set_input_value(password_input, self.pd)
            logging.info("输入密码")
            logging.debug("Password field value length=%s", len(password_input.get_attribute("value") or ""))

            self.wait.until(
                lambda driver: login_button.is_enabled()
                and "disabled" not in (login_button.get_attribute("class") or "")
            )
            logging.info("点击登录按钮")
            login_button.click()

            try:
                self.wait.until(
                    lambda driver: "hdu.huitu.zhishulib.com" in driver.current_url
                )
            except TimeoutException:
                logging.warning(
                    "Login did not redirect to target site, current_url=%s",
                    self.driver.current_url,
                )

            cookie_list = self.driver.get_cookies()
            self.cookie_items = cookie_list
            self._refresh_cookie_header_from_items()
            self.cfg["headers"]["Cookie"] = self.cookie

            library_cookies = [
                item for item in cookie_list
                if "hdu.huitu.zhishulib.com" in item.get("domain", "")
            ]
            if "hdu.huitu.zhishulib.com" not in self.driver.current_url and not library_cookies:
                raise RuntimeError(
                    "登录后未跳回图书馆站点，且未获取到图书馆域名Cookie"
                )
            if not self.cookie:
                raise RuntimeError("登录完成但未获取到Cookie")

            logging.info("登录成功")
        except Exception as exc:
            self._dump_login_debug()
            logging.error("登录失败：%s", exc)
            return -1
        return 0

    def get_user_info(self):
        logging.info("Getting user info")

        session = self._build_requests_session()
        search_url = "https://hdu.huitu.zhishulib.com/Seat/Index/searchSeats?LAB_JSON=1"
        try:
            resp = session.get(search_url)
            resp_json = resp.json()
            if resp_json.get("ui_type") == "com.Redirect" and resp_json.get("href"):
                redirect_url = resp_json["href"]
                logging.warning("searchSeats requested CAS redirect: %s", redirect_url)
                redirect_resp = session.get(redirect_url, allow_redirects=True)
                logging.info(
                    "CAS bridge response status=%s final_url=%s",
                    redirect_resp.status_code,
                    redirect_resp.url,
                )
                self._sync_cookie_items_from_session(session)
                self.cfg["headers"]["Cookie"] = self.cookie
                resp = session.get(search_url)
                resp_json = resp.json()
            if "DATA" not in resp_json:
                logging.error("searchSeats response missing DATA: %s", resp.text[:500])
                self._dump_user_info_debug(resp)
                raise KeyError("DATA")
            self._sync_cookie_items_from_session(session)
            self.cfg["headers"]["Cookie"] = self.cookie
            self.user_data = resp_json["DATA"]
            _ = self.user_data["uid"]
        except Exception as exc:
            logging.exception(exc)
            print(self.user_data)
            print(exc.__class__.__name__ + ",获取用户数据失败")
            return -1
        print("获取用户数据成功")
        return 0

    def wechatNotice(self, message, desp=None):
        logging.info("Sending WeChat notice")

        if self.SCKey:
            url = "https://sctapi.ftqq.com/{0}.send".format(self.SCKey)
            data = {
                "title": message,
                desp: desp,
            }
            try:
                response = requests.post(url, data=data)
                if response.json()["data"]["error"] == "SUCCESS":
                    print("Server酱通知成功")
                else:
                    print("Server酱通知失败")
            except Exception as exc:
                logging.exception(exc)
                print(exc.__class__, "推送服务配置错误")


def is_booking_enable(date_cfg):
    return bool(date_cfg["启用"])


if __name__ == "__main__":
    logging.info("Start of the program")
    with open("user_config.yml", "r", encoding="utf-8") as f_obj:
        user_config = yaml.safe_load(f_obj)
    with open("config/basic_config.yml", "r", encoding="utf-8") as f_obj:
        basic_config = yaml.safe_load(f_obj)
    with open("config/seat_config.yml", "r", encoding="utf-8") as f_obj:
        seat_config = yaml.safe_load(f_obj)

    target_weekday = get_target_weekday(offset_days=2)
    if not is_booking_enable(user_config[target_weekday]):
        logging.info("预约未启用")
        print("预约未启用")
        raise SystemExit(0)

    seat_booker = SeatAutoBooker(basic_config["SeatAutoBooker"])
    if seat_booker.login() != 0:
        seat_booker.driver.quit()
        logging.info("Login unsuccessful")
        raise SystemExit(-1)
    if seat_booker.get_user_info() != 0:
        seat_booker.driver.quit()
        logging.info("Getting user info unsuccessful")
        raise SystemExit(-1)
    seat_booker.book_favorite_seat(user_config=user_config, seat_config=seat_config)
    seat_booker.driver.quit()
    logging.info("End of the program")
