import asyncio
import io
import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytesseract
import schedule
import smtplib
import undetected_chromedriver as uc
from email.message import EmailMessage
from PIL import Image
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('booking_agent.log'),
        logging.StreamHandler()
    ]
)

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

@dataclass
class BookingRequest:
    id: str
    movie_name: str
    date: str
    preferred_time_range: List[str]
    cinemas: List[str]
    city: str
    max_price: float
    priority: int
    auto_book: bool
    status: str
    created_at: str
    movie_url: Optional[str] = None

class MovieBookingAgent:
    def __init__(self, config_file='config.json'):
        self.config = self.load_config(config_file)
        self.driver = None
        self.booking_requests = []
        self.load_booking_requests()
        
    def load_config(self, config_file):
        """Load configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Failed to load config: {e}")
            return {}
    
    def save_config(self):
        """Save current configuration to file"""
        try:
            with open('config.json', 'w') as f:
                json.dump(self.config, f, indent=2)
            logging.info("Configuration saved successfully")
        except Exception as e:
            logging.error(f"Failed to save config: {e}")
    
    def load_booking_requests(self):
        """Load booking requests from config"""
        self.booking_requests = []
        for req_data in self.config.get('booking_requests', []):
            req = BookingRequest(**req_data)
            self.booking_requests.append(req)
    
    def add_booking_request(self, movie_name, date, time_range, cinemas, city='Coimbatore', 
                          max_price=300, auto_book=True):
        """Add a new booking request"""
        req_id = f"req_{len(self.booking_requests) + 1:03d}"
        new_request = BookingRequest(
            id=req_id,
            movie_name=movie_name,
            date=date,
            preferred_time_range=time_range,
            cinemas=cinemas,
            city=city,
            max_price=max_price,
            priority=1,
            auto_book=auto_book,
            status='monitoring',
            created_at=datetime.now().isoformat()
        )
        
        self.booking_requests.append(new_request)
        
        # Add to config
        self.config['booking_requests'].append({
            'id': new_request.id,
            'movie_name': new_request.movie_name,
            'date': new_request.date,
            'movie_url': new_request.movie_url,
            'preferred_time_range': new_request.preferred_time_range,
            'cinemas': new_request.cinemas,
            'city': new_request.city,
            'max_price': new_request.max_price,
            'priority': new_request.priority,
            'auto_book': new_request.auto_book,
            'status': new_request.status,
            'created_at': new_request.created_at
        })
        
        self.save_config()
        logging.info(f"Added booking request: {req_id} for {movie_name}")
        return req_id
    
    def setup_driver(self):
        """Setup Chrome driver with optimized options"""
        options = uc.ChromeOptions()
        
        if self.config['system_settings']['headless_browser']:
            options.add_argument("--headless=new")
        
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument(f"user-agent={random.choice(self.config['system_settings']['user_agents'])}")
        
        # Performance optimizations
        prefs = {
            "profile.managed_default_content_settings.images": 2 if not self.config['system_settings']['screenshot_enabled'] else 1,
            "profile.managed_default_content_settings.stylesheets": 2,
            "profile.managed_default_content_settings.fonts": 2,
        }
        options.add_experimental_option("prefs", prefs)
        
        self.driver = uc.Chrome(options=options)
        self.driver.set_window_size(1920, 1080)
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    def find_movie_url(self, movie_name, date, city, movie_url_override=None):
        """Find the booking URL for a specific movie.

        If ``movie_url_override`` is provided (from config), use it directly
        — no slug‑guessing needed.  Otherwise fall back to constructing
        URLs from the movie name.
        """
        # --- Direct URL from config (preferred) --------------------------
        if movie_url_override:
            logging.info(f"Using movie URL from config: {movie_url_override}")
            if self.driver:
                self.driver.get(movie_url_override)
                time.sleep(3)
                # The page may redirect — use the final URL
                current = self.driver.current_url
                logging.info(f"Final URL after navigation: {current}")
                return current
            return movie_url_override

        # --- Fallback: guess slug from movie name ------------------------
        movie_slug = movie_name.lower().replace(' ', '-')
        date_formatted = date.replace('-', '')

        search_urls = [
            f"https://in.bookmyshow.com/movies/{city.lower()}/{movie_slug}",
            f"https://in.bookmyshow.com/{city.lower()}/movies/{movie_slug}",
        ]

        for url in search_urls:
            try:
                if self.driver:
                    self.driver.get(url)
                    time.sleep(3)

                    if "book tickets" in self.driver.page_source.lower():
                        booking_links = self.driver.find_elements(By.PARTIAL_LINK_TEXT, "Book")
                        for link in booking_links:
                            href = link.get_attribute('href')
                            if date_formatted in href:
                                return href
            except Exception as e:
                logging.warning(f"Error checking URL {url}: {e}")
                continue

        return None
    
    def check_availability(self, booking_url):
        """Check if booking is available for a given URL"""
        if not self.driver:
            self.setup_driver()
        
        try:
            logging.info(f"Checking availability for: {booking_url}")
            self.driver.get(booking_url)
            time.sleep(random.uniform(5, 8))
            
            # Take screenshot for OCR analysis
            if self.config['system_settings']['screenshot_enabled']:
                screenshot = self.driver.get_screenshot_as_png()
                image = Image.open(io.BytesIO(screenshot))
                extracted_text = pytesseract.image_to_string(image).strip().lower()
                
                logging.info(f"OCR Text: {extracted_text[:200]}")
                
                # Check for blocking keywords
                blocked_keywords = [
                    "uh-oh! we couldn't find anything",
                    "you are unable to access",
                    "captcha",
                    "please verify",
                    "blocked",
                    "access denied",
                    "unusual traffic",
                    "security service",
                    "automated access",
                    "are you a robot"
                ]
                
                if any(keyword in extracted_text for keyword in blocked_keywords):
                    logging.warning("Blocked or shows not available detected")
                    return False
            
            # Check for cinema availability using page elements
            cinema_elements = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='cinema-name'], .cinema-name, h3")
            available_cinemas = []
            
            for element in cinema_elements:
                try:
                    text = element.text.strip()
                    if text:
                        available_cinemas.append(text.lower())
                except:
                    continue
            
            logging.info(f"Found cinemas: {available_cinemas}")
            
            # Check if any of our target cinemas are available
            target_cinemas = [cinema.lower() for cinema in self.config['cinema_database'].get(self.config['user_profile']['city'], {}).keys()]
            
            for cinema in available_cinemas:
                for target in target_cinemas:
                    if target in cinema:
                        logging.info(f"Found target cinema: {cinema}")
                        return True
                        
            return False
            
        except Exception as e:
            logging.error(f"Error checking availability: {e}")
            return False
    
    def book_tickets(self, booking_request: BookingRequest):
        """Attempt to book tickets for a specific request"""
        if not booking_request.auto_book:
            logging.info(f"Auto-booking disabled for {booking_request.id}")
            return False
        
        try:
            # Find the movie URL
            movie_url = self.find_movie_url(
                booking_request.movie_name, 
                booking_request.date, 
                booking_request.city
            )
            
            if not movie_url:
                logging.error(f"Could not find movie URL for {booking_request.movie_name}")
                return False
            
            # Check availability
            if not self.check_availability(movie_url):
                return False
            
            logging.info(f"Attempting to book tickets for {booking_request.movie_name}")
            
            # Select cinema and showtime
            success = self.select_cinema_and_showtime(booking_request)
            if not success:
                return False
            
            # Select seats
            success = self.select_seats()
            if not success:
                return False
            
            # Complete payment (this would need to be implemented based on your payment method)
            # success = self.complete_payment()
            # For now, we'll just log and send notification
            
            logging.info(f"Booking process initiated for {booking_request.movie_name}")
            self.send_booking_notification(booking_request, "Booking process started")
            
            return True
            
        except Exception as e:
            logging.error(f"Error during booking: {e}")
            return False
    
    def select_cinema_and_showtime(self, booking_request: BookingRequest):
        """Select preferred cinema and showtime"""
        try:
            # Wait for cinema elements to load
            wait = WebDriverWait(self.driver, 10)
            
            # Look for cinema containers
            cinema_containers = wait.until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "[data-testid='cinema-container'], .cinema-container"))
            )
            
            for container in cinema_containers:
                try:
                    cinema_name = container.find_element(By.CSS_SELECTOR, "h3, .cinema-name").text.lower()
                    
                    # Check if this is one of our preferred cinemas
                    if any(preferred.lower() in cinema_name for preferred in booking_request.cinemas):
                        logging.info(f"Found preferred cinema: {cinema_name}")
                        
                        # Look for showtimes
                        showtime_buttons = container.find_elements(By.CSS_SELECTOR, "button[data-testid='showtime'], .showtime-button")
                        
                        for button in showtime_buttons:
                            showtime = button.text
                            # Check if showtime matches preferred range
                            if self.is_preferred_showtime(showtime, booking_request.preferred_time_range):
                                logging.info(f"Selecting showtime: {showtime}")
                                button.click()
                                time.sleep(2)
                                return True
                                
                except Exception as e:
                    logging.warning(f"Error processing cinema container: {e}")
                    continue
            
            return False
            
        except TimeoutException:
            logging.error("Timeout waiting for cinema elements")
            return False
    
    def is_preferred_showtime(self, showtime, preferred_ranges):
        """Check if showtime falls within preferred ranges"""
        try:
            # Extract time from showtime string
            time_match = re.search(r'(\d{1,2}):(\d{2})', showtime)
            if not time_match:
                return False
            
            hour = int(time_match.group(1))
            
            for range_name in preferred_ranges:
                range_times = self.config['user_profile']['preferred_showtimes'].get(range_name, [])
                for range_time in range_times:
                    range_hour = int(range_time.split(':')[0])
                    if abs(hour - range_hour) <= 1:  # Within 1 hour of preferred time
                        return True
            
            return False
            
        except Exception as e:
            logging.error(f"Error checking showtime: {e}")
            return False
    
    def select_seats(self):
        """Select preferred seats"""
        try:
            wait = WebDriverWait(self.driver, 15)
            
            # Wait for seat map to load
            seat_map = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='seat-map'], .seat-map, #seatLayoutContainer"))
            )
            
            preferred_seats = self.config['user_profile']['preferred_seats']
            max_tickets = self.config['user_profile']['max_tickets']
            selected_count = 0
            
            for seat_id in preferred_seats:
                if selected_count >= max_tickets:
                    break
                
                try:
                    # Try different seat selector patterns
                    seat_selectors = [
                        f"[data-seat-id='{seat_id}']",
                        f"#{seat_id}",
                        f"[aria-label*='{seat_id}']",
                        f"[title*='{seat_id}']"
                    ]
                    
                    for selector in seat_selectors:
                        try:
                            seat = self.driver.find_element(By.CSS_SELECTOR, selector)
                            if seat.is_enabled() and "available" in seat.get_attribute("class").lower():
                                seat.click()
                                selected_count += 1
                                logging.info(f"Selected seat: {seat_id}")
                                time.sleep(0.5)
                                break
                        except NoSuchElementException:
                            continue
                            
                except Exception as e:
                    logging.warning(f"Could not select seat {seat_id}: {e}")
                    continue
            
            if selected_count > 0:
                # Look for continue/proceed button
                proceed_buttons = self.driver.find_elements(By.CSS_SELECTOR, 
                    "button[data-testid='proceed'], .proceed-btn, #proceed, button:contains('Proceed')")
                
                for button in proceed_buttons:
                    if button.is_enabled():
                        button.click()
                        logging.info(f"Proceeded with {selected_count} seats selected")
                        return True
            
            return False
            
        except TimeoutException:
            logging.error("Timeout waiting for seat map")
            return False
    
    def send_booking_notification(self, booking_request: BookingRequest, message):
        """Send booking notification"""
        if not self.config['notification_settings']['email_notifications']:
            return
        
        subject = f"🎬 Booking Update: {booking_request.movie_name}"
        body = f"""
        Movie: {booking_request.movie_name}
        Date: {booking_request.date}
        Cinemas: {', '.join(booking_request.cinemas)}
        Status: {message}
        
        Booking ID: {booking_request.id}
        Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """
        
        for recipient in self.config['notification_settings']['notification_recipients']:
            self.send_email(recipient, subject, body)
    
    def send_email(self, recipient, subject, body):
        """Send email notification"""
        try:
            msg = EmailMessage()
            msg['Subject'] = subject
            msg['From'] = self.config['user_profile']['email']
            msg['To'] = recipient
            msg.set_content(body)
            
            # You'll need to configure email settings
            email_password = "your_app_password"  # Use environment variable or secure storage
            
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
                smtp.login(self.config['user_profile']['email'], email_password)
                smtp.send_message(msg)
                logging.info(f"Email sent to {recipient}")
        except Exception as e:
            logging.error(f"Failed to send email: {e}")
    
    def _extract_booking_url(self, current_url: str, target_date: str) -> Optional[str]:
        """Extract or construct the direct booking URL from the current page.

        Parameters
        ----------
        current_url : str
            The current driver URL.
        target_date : str
            ``YYYY-MM-DD`` date to inject into the URL.

        Returns
        -------
        str or None
            The full booking URL, or ``None`` if the current URL pattern
            doesn't match any known format.
        """
        # Already on a buytickets page?
        match = re.search(
            r'/movies/([^/]+)/([^/]+)/buytickets/(ET\d+)/(\d{8})',
            current_url,
        )
        if match:
            return current_url

        # On a movie page with ET code: /movies/{city}/{slug}/{ET}
        match = re.search(
            r'/movies/([^/]+)/([^/]+)/(ET\d+)',
            current_url,
        )
        if match:
            city, slug, et_code = match.groups()
            date_formatted = target_date.replace('-', '')
            return (
                f"https://in.bookmyshow.com/movies/{city}/{slug}"
                f"/buytickets/{et_code}/{date_formatted}"
            )

        # Try to find any ET code in the page source
        try:
            source = self.driver.page_source
            match = re.search(r'(ET\d+)', source)
            if match:
                et_code = match.group(1)
                movie_slug = (
                    target_date.replace('-', '')  # fallback — won't be right
                )
                logging.warning(
                    "Partial URL extraction — ET code: %s, "
                    "but couldn't determine city/slug.", et_code
                )
            return None
        except Exception:
            return None

    def monitor_bookings(self, use_ai_bridge: bool = True):
        """Monitor all active booking requests"""
        logging.info("Starting booking monitor...")
        
        active_requests = [req for req in self.booking_requests if req.status == 'monitoring']
        
        if not active_requests:
            logging.info("No active booking requests")
            return
        
        self.setup_driver()
        
        try:
            for request in active_requests:
                logging.info(f"Checking request: {request.id} - {request.movie_name}")
                
                # Check if booking date has passed
                booking_date = datetime.strptime(request.date, '%Y-%m-%d').date()
                if booking_date < datetime.now().date():
                    request.status = 'expired'
                    logging.info(f"Request {request.id} expired")
                    continue
                
                # --- AI Bridge path (when auto_book enabled) -----------------
                if use_ai_bridge and request.auto_book:
                    logging.info(
                        f"[{request.id}] auto_book enabled — using AI bridge."
                    )
                    try:
                        movie_url = self.find_movie_url(
                            request.movie_name,
                            request.date,
                            request.city,
                            movie_url_override=getattr(request, 'movie_url', None),
                        )
                        if not movie_url:
                            logging.warning(
                                f"[{request.id}] Could not find movie URL."
                            )
                            continue

                        booking_url = self._extract_booking_url(
                            movie_url, request.date
                        )
                        if not booking_url:
                            logging.warning(
                                f"[{request.id}] Could not extract booking URL "
                                f"from {movie_url}"
                            )
                            continue

                        if not self.check_availability(booking_url):
                            logging.info(
                                f"[{request.id}] Not available yet."
                            )
                            continue

                        # Extract time window from request
                        time_ranges = request.preferred_time_range
                        window: tuple[int, int] | None = None
                        if time_ranges:
                            showtimes = (
                                self.config.get("user_profile", {})
                                .get("preferred_showtimes", {})
                            )
                            hours = []
                            for key in time_ranges:
                                for slot in showtimes.get(key, []):
                                    try:
                                        h = int(slot.strip().split(":")[0])
                                        hours.append(h)
                                    except (ValueError, IndexError):
                                        pass
                            if hours:
                                window = (min(hours), max(hours) + 1)

                        alert_data = {
                            "movie_name": request.movie_name,
                            "cinema": request.cinemas[0] if request.cinemas else "",
                            "date": request.date,
                            "booking_url": booking_url,
                            "unique_code": (
                                "ET" + booking_url.split("ET")[-1][:8]
                                if "ET" in booking_url else ""
                            ),
                            "time_window": window,
                            "num_tickets": self.config.get("user_profile", {}).get("max_tickets", 2),
                            "city": request.city,
                        }

                        logging.info(
                            "[%s] 🎯 Triggering AI agent via bridge: %s",
                            request.id, booking_url,
                        )

                        # The monitor runs in a thread — run async bridge
                        from alert_to_agent import AlertToAgentBridge
                        bridge = AlertToAgentBridge(self.config)

                        result = asyncio.run(
                            bridge.on_booking_detected(alert_data)
                        )

                        if result and result.get("success"):
                            request.status = 'booked'
                            self.send_booking_notification(
                                request, "✅ AI agent booking successful!"
                            )
                        else:
                            request.status = 'booking_failed'
                            self.send_booking_notification(
                                request,
                                f"❌ AI booking failed: {result.get('error') if result else 'no result'}",
                            )
                    except Exception as e:
                        logging.error(
                            "[%s] AI bridge error: %s", request.id, e
                        )
                        request.status = 'booking_failed'

                # --- Legacy path (no auto_book or bridge disabled) ----------
                else:
                    success = self.book_tickets(request)
                    if success:
                        request.status = 'booking_initiated'
                        self.send_booking_notification(
                            request, "Booking initiated successfully!"
                        )

                # Add delay between requests
                time.sleep(random.uniform(10, 20))
                
        except Exception as e:
            logging.error(f"Error in monitor_bookings: {e}")
        
        finally:
            if self.driver:
                self.driver.quit()
                self.driver = None
    
    def start_monitoring(self):
        """Start the continuous monitoring process"""
        def run_monitor():
            while True:
                try:
                    self.monitor_bookings()
                    time.sleep(self.config['system_settings']['check_interval_seconds'])
                except Exception as e:
                    logging.error(f"Error in monitoring loop: {e}")
                    time.sleep(60)  # Wait a minute before retrying
        
        # Start monitoring in a separate thread
        monitor_thread = threading.Thread(target=run_monitor, daemon=True)
        monitor_thread.start()
        
        logging.info("Monitoring started in background")
        return monitor_thread

# Example usage and CLI interface
def main():
    agent = MovieBookingAgent()
    
    print("🎬 Movie Booking Agent Started")
    print("Commands:")
    print("1. add <movie_name> <date> <time_range> <cinemas>")
    print("2. list - Show all booking requests")
    print("3. monitor - Start monitoring")
    print("4. quit - Exit")
    
    while True:
        try:
            command = input("\n> ").strip().lower()
            
            if command == 'quit':
                break
            elif command == 'list':
                print("\nActive Booking Requests:")
                for req in agent.booking_requests:
                    print(f"ID: {req.id}, Movie: {req.movie_name}, Date: {req.date}, Status: {req.status}")
            elif command == 'monitor':
                print("Starting monitoring...")
                agent.start_monitoring()
                input("Press Enter to stop monitoring...")
            elif command.startswith('add'):
                # Example: add Coolie 2025-08-15 evening PVR,INOX
                parts = command.split()
                if len(parts) >= 5:
                    movie_name = parts[1]
                    date = parts[2]
                    time_range = [parts[3]]
                    cinemas = parts[4].split(',')
                    
                    req_id = agent.add_booking_request(movie_name, date, time_range, cinemas)
                    print(f"Added booking request: {req_id}")
                else:
                    print("Usage: add <movie_name> <date> <time_range> <cinemas>")
            else:
                print("Unknown command")
                
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
    
    print("Goodbye!")

if __name__ == "__main__":
    main()