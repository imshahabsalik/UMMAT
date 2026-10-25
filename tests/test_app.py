import os
import struct
import time
import unittest
import zlib
from io import BytesIO
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent / ".runtime"
TEST_DIR.mkdir(parents=True, exist_ok=True)
os.environ["UMMAT_DATABASE_PATH"] = str(TEST_DIR / "test.sqlite3")
os.environ["UMMAT_EMAIL_BACKEND"] = "console"

import app as site
from PIL import Image


class AppFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site.UPLOAD_DIR = str(TEST_DIR / "uploads")
        os.makedirs(site.UPLOAD_DIR, exist_ok=True)
        site.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)

    def setUp(self):
        with site.write_transaction():
            site._save_stores_conn(site._transaction.connection, [])
            site._save_enquiries_conn(site._transaction.connection, [])
            site._transaction.connection.execute("DELETE FROM account_tokens")
            site._transaction.connection.execute("DELETE FROM rate_limits")
        self.client = site.app.test_client()

    def csrf(self):
        self.client.get("/login")
        with self.client.session_transaction() as sess:
            return sess["_csrf_token"]

    def register(self, email="supplier@example.com"):
        token = self.csrf()
        data = {
            "_csrf_token": token, "personal_name": "Test Person",
            "business_name": "Test Supplier", "category": "Hardware",
            "location": "Delhi", "description": "", "phone": "", "account_email": email,
            "account_password": "correct horse battery", "account_password_confirm": "correct horse battery",
            "product_name": ["Bolts"],
            "product_price": ["12"], "product_unit": ["piece"], "product_moq": ["20"],
            "product_description": ["Steel bolts"], "product_image": [(BytesIO(), "")],
        }
        return self.client.post("/create-store", data=data, content_type="multipart/form-data")

    def make_png(self, size=(40, 30)):
        stream = BytesIO()
        Image.new("RGB", size, "#356789").save(stream, format="PNG")
        return stream.getvalue()

    def test_registration_verification_login_and_password_reset(self):
        response = self.register()
        self.assertEqual(response.status_code, 302)
        self.assertIn("registered=1", response.location)
        response = self.client.post("/login", data={"_csrf_token": self.csrf(), "email": "supplier@example.com",
                                                     "password": "correct horse battery"})
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"/resend-verification", response.data)
        with site.write_transaction():
            token = site._issue_token(site._transaction.connection, "supplier@example.com", "verify", 3600)
        response = self.client.post(f"/verify-email/{token}", data={"_csrf_token": self.csrf()})
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/login", data={"_csrf_token": self.csrf(), "email": "supplier@example.com",
                                                     "password": "correct horse battery"})
        self.assertEqual(response.status_code, 302)
        with site.write_transaction():
            reset = site._issue_token(site._transaction.connection, "supplier@example.com", "reset", 3600)
        response = self.client.post(f"/reset-password/{reset}", data={"_csrf_token": self.csrf(),
                                                                       "password": "different horse battery",
                                                                       "confirm_password": "different horse battery"})
        self.assertEqual(response.status_code, 302)
        self.client.post("/logout", data={"_csrf_token": self.csrf()})
        response = self.client.post("/login", data={"_csrf_token": self.csrf(), "email": "supplier@example.com",
                                                     "password": "different horse battery"})
        self.assertEqual(response.status_code, 302)

    def test_image_decode_resize_logo_and_buyer_inbox(self):
        image = self.make_png((1800, 1200))
        data, ext, error = site.read_image(type("Upload", (), {"filename": "logo.png", "stream": BytesIO(image)})())
        self.assertIsNone(error)
        self.assertEqual(ext, "png")
        with Image.open(BytesIO(data)) as output:
            self.assertEqual(output.size, (1600, 1067))
        response = self.register("logos@example.com")
        self.assertEqual(response.status_code, 302)
        # Attach a decoded supplier logo to exercise the public display path.
        stores = site.load_stores()
        stores[0]["logo"] = "uploads/test.png"
        with open(Path(site.UPLOAD_DIR) / "test.png", "wb") as f:
            f.write(data)
        site.save_stores(stores)
        self.client.post("/api/enquiry", headers={"X-CSRF-Token": self.csrf()},
                          json={"store_slug": stores[0]["slug"], "buyer_name": "Buyer",
                                "contact": "buyer@example.com", "quantity": "50",
                                "message": "Need a quote"})
        self.client.get("/login")
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["supplier_email"] = "logos@example.com"
        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Need a quote", dashboard.data)

    def test_large_image_is_resized_promptly(self):
        source = BytesIO()
        Image.new("RGB", (4800, 3200), "#356789").save(source, format="JPEG", quality=88)
        upload = type("Upload", (), {"filename": "large.jpg", "stream": BytesIO(source.getvalue())})()
        started = time.perf_counter()
        data, ext, error = site.read_image(upload)
        elapsed = time.perf_counter() - started
        self.assertIsNone(error)
        self.assertEqual(ext, "jpg")
        self.assertLess(elapsed, 8.0, f"Image processing took {elapsed:.2f}s")
        with Image.open(BytesIO(data)) as output:
            self.assertEqual(output.size, (1600, 1067))

    def test_transparent_png_keeps_alpha(self):
        source = BytesIO()
        Image.new("RGBA", (64, 48), (20, 80, 140, 96)).save(source, format="PNG")
        upload = type("Upload", (), {"filename": "transparent.png", "stream": BytesIO(source.getvalue())})()
        data, ext, error = site.read_image(upload)
        self.assertIsNone(error)
        self.assertEqual(ext, "png")
        with Image.open(BytesIO(data)) as output:
            self.assertEqual(output.mode, "RGBA")
            self.assertEqual(output.getpixel((0, 0))[3], 96)

    def test_image_over_pixel_limit_is_rejected_before_decode(self):
        png = bytearray(self.make_png())
        png[16:24] = struct.pack(">II", 10_000, 5_000)
        png[29:33] = struct.pack(">I", zlib.crc32(png[12:29]) & 0xffffffff)
        upload = type("Upload", (), {"filename": "huge.png", "stream": BytesIO(png)})()
        data, ext, error = site.read_image(upload)
        self.assertIsNone(data)
        self.assertIsNone(ext)
        self.assertIn("under 40 megapixels", error)

    def test_bad_image_and_csrf_are_rejected(self):
        bad = type("Upload", (), {"filename": "fake.png", "stream": BytesIO(b"not an image")})()
        self.assertIn("valid JPG, PNG or WebP", site.read_image(bad)[2])
        valid = self.make_png()
        exact_limit = valid + bytes([0]) * (site.MAX_IMAGE_BYTES - len(valid))
        accepted = type("Upload", (), {"filename": "five-mb.png", "stream": BytesIO(exact_limit)})()
        self.assertIsNone(site.read_image(accepted)[2])
        oversized = type("Upload", (), {"filename": "too-large.png",
                                          "stream": BytesIO(exact_limit + bytes([0]))})()
        self.assertIn("larger than 5 MB", site.read_image(oversized)[2])
        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 400)

    def test_registration_rejects_mismatched_password_confirmation(self):
        token = self.csrf()
        data = {"_csrf_token": token, "personal_name": "Mismatch Person",
                "business_name": "Mismatch Ltd", "category": "Hardware",
                "location": "Delhi", "account_email": "mismatch@example.com",
                "account_password": "correct horse battery", "account_password_confirm": "different password",
                "product_name": ["Bolts"], "product_price": ["12"], "product_unit": ["piece"],
                "product_moq": ["20"], "product_description": [""], "product_image": [(BytesIO(), "")],
                "store_logo": (BytesIO(), "")}
        response = self.client.post("/create-store", data=data, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"passwords do not match", response.data)
        self.assertNotIn("mismatch@example.com", [s.get("owner_email") for s in site.load_stores()])

    def test_production_registration_requires_mail_configuration(self):
        keys = ("UMMAT_ENV", "UMMAT_SMTP_HOST", "UMMAT_EMAIL_FROM")
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["UMMAT_ENV"] = "production"
            os.environ.pop("UMMAT_SMTP_HOST", None)
            os.environ.pop("UMMAT_EMAIL_FROM", None)
            response = self.register("blocked@example.com")
            self.assertEqual(response.status_code, 503)
            self.assertIn(b"Account email is temporarily unavailable", response.data)
            self.assertFalse(any(s.get("owner_email") == "blocked@example.com" for s in site.load_stores()))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_login_rate_limit(self):
        token = self.csrf()
        statuses = [self.client.post("/login", data={"_csrf_token": token, "email": "nobody@example.com",
                                                      "password": "wrong"}).status_code for _ in range(11)]
        self.assertEqual(statuses[-1], 429)

    def test_supplier_can_delete_their_store_and_enquiries(self):
        self.register("delete@example.com")
        stores = site.load_stores()
        target = stores[0]
        target["logo"] = "uploads/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"
        with open(Path(site.UPLOAD_DIR) / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png", "wb") as f:
            f.write(self.make_png())
        site.save_stores(stores)
        site.save_enquiries([{"id": "delete-enquiry", "store_slug": target["slug"]}])
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["supplier_email"] = "delete@example.com"
            sess["_csrf_token"] = "delete-csrf"
        response = self.client.post("/manage-store/delete", data={"_csrf_token": "delete-csrf"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(target["slug"], [s["slug"] for s in site.load_stores()])
        self.assertFalse(site.load_enquiries())
        self.assertFalse((Path(site.UPLOAD_DIR) / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png").exists())


if __name__ == "__main__":
    unittest.main()
