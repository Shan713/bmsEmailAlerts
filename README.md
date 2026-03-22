# Movie Booking Agent 🎬🤖

An intelligent, automated movie ticket booking agent for BookMyShow that monitors ticket availability and books tickets automatically based on your preferences.

## Features

- **🔍 Intelligent Monitoring**: Continuously monitors BookMyShow for ticket availability
- **🎯 Smart Booking**: Automatically books tickets when they become available
- **🎭 Multi-Movie Support**: Track and book multiple movies simultaneously
- **🏛️ Cinema Preferences**: Support for multiple cinema chains (PVR, INOX, Cinepolis, etc.)
- **⏰ Time Preferences**: Set preferred show times (morning, afternoon, evening, night)
- **💺 Seat Selection**: Automatic selection of your preferred seats
- **📧 Notifications**: Email alerts for booking status updates
- **🔒 Secure Credentials**: Encrypted storage of login and payment information
- **🖥️ CLI Interface**: Easy command-line interface for management
- **📊 Configuration Management**: Flexible JSON-based configuration system

## Installation

### Prerequisites

1. **Python 3.8+** installed on your system
2. **Chrome Browser** (latest version)
3. **Tesseract OCR** installed:
   - Windows: Download from [GitHub releases](https://github.com/UB-Mannheim/tesseract/wiki)
   - Install to: `C:\Program Files\Tesseract-OCR\`

### Setup

1. **Clone or download** this repository:
   ```bash
   cd c:\Users\shant\OneDrive\Documents\GitHub\bmsEmailAlerts
   ```

2. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Setup your credentials** (secure, encrypted storage):
   ```bash
   python booking_cli.py setup-credentials
   ```

4. **Configure your preferences**:
   ```bash
   # Edit config.json file or use CLI
   python booking_cli.py config --show
   ```

## Quick Start

### 1. Add a Movie to Monitor

```bash
# Basic usage
python booking_cli.py add "Coolie" "2025-08-15"

# With specific preferences
python booking_cli.py add "Coolie" "2025-08-15" \
  --time-range "evening,night" \
  --cinemas "PVR Brookfields,INOX Prozone" \
  --max-price 350 \
  --city "Coimbatore"
```

### 2. View Your Bookings

```bash
# List all bookings
python booking_cli.py list

# Detailed view
python booking_cli.py list --verbose
```

### 3. Start Monitoring

```bash
# Start continuous monitoring
python booking_cli.py monitor
```

### 4. Check Single Movie

```bash
# Check if tickets are available now
python booking_cli.py check "Coolie" "2025-08-15" --city "Coimbatore"
```

## Configuration

### Main Configuration File: `config.json`

```json
{
  "user_profile": {
    "email": "your_email@gmail.com",
    "phone": "your_phone_number",
    "name": "Your Name",
    "preferred_seats": ["H8", "H9", "G8", "G9", "I8", "I9"],
    "max_tickets": 2,
    "preferred_cinema_chains": ["PVR", "INOX", "Cinepolis"],
    "preferred_showtimes": {
      "morning": ["10:00", "11:00", "12:00"],
      "afternoon": ["13:00", "14:00", "15:00", "16:00"],
      "evening": ["18:00", "19:00", "20:00", "21:00"],
      "night": ["22:00", "23:00"]
    }
  },
  "cinema_database": {
    "Coimbatore": {
      "PVR Brookfields": {
        "base_url": "https://in.bookmyshow.com/coimbatore/cinemas/pvr-brookfields/PVRB",
        "preferred_seats": ["H8", "H9", "G8", "G9"]
      }
    }
  },
  "system_settings": {
    "check_interval_seconds": 30,
    "headless_browser": false,
    "screenshot_enabled": true
  }
}
```

### Update Configuration via CLI

```bash
# Set check interval
python booking_cli.py config --set "system_settings.check_interval_seconds=60"

# Update preferred seats
python booking_cli.py config --set "user_profile.preferred_seats=[\"G8\",\"G9\",\"H8\",\"H9\"]"
```

## Advanced Usage

### Multiple Movies Monitoring

Add multiple movies to monitor simultaneously:

```bash
python booking_cli.py add "Coolie" "2025-08-15" --time-range "evening" --auto-book
python booking_cli.py add "Pushpa 2" "2025-08-20" --time-range "night" --auto-book
python booking_cli.py add "KGF 3" "2025-08-25" --time-range "morning,evening" --no-auto-book
```

### Notification-Only Mode

Set up monitoring without automatic booking (notifications only):

```bash
python booking_cli.py add "Coolie" "2025-08-15" --no-auto-book
```

### City-Specific Configuration

For different cities, you can:

1. Update `cinema_database` in config.json
2. Add city-specific cinema information
3. Use `--city` parameter when adding bookings

### Seat Preferences

Configure your preferred seats in `config.json`:

```json
{
  "user_profile": {
    "preferred_seats": ["H8", "H9", "G8", "G9", "I8", "I9"],
    "max_tickets": 2
  }
}
```

## CLI Commands Reference

### Core Commands

| Command | Description | Example |
|---------|-------------|---------|
| `add` | Add new booking request | `python booking_cli.py add "Movie Name" "2025-08-15"` |
| `list` | List all booking requests | `python booking_cli.py list --verbose` |
| `monitor` | Start continuous monitoring | `python booking_cli.py monitor` |
| `check` | Check single movie availability | `python booking_cli.py check "Movie" "2025-08-15"` |
| `remove` | Remove booking request | `python booking_cli.py remove req_001` |

### Management Commands

| Command | Description | Example |
|---------|-------------|---------|
| `setup-credentials` | Setup encrypted credentials | `python booking_cli.py setup-credentials` |
| `config --show` | Show current configuration | `python booking_cli.py config --show` |
| `config --set` | Update configuration | `python booking_cli.py config --set "key=value"` |

### Command Options

**Add Command Options:**
- `--time-range`: Preferred show times (morning,afternoon,evening,night)
- `--cinemas`: Preferred cinemas (comma-separated)
- `--city`: City name
- `--max-price`: Maximum ticket price
- `--auto-book` / `--no-auto-book`: Enable/disable automatic booking

## How It Works

### 1. Monitoring Process

1. **URL Generation**: Constructs BookMyShow URLs based on movie name, date, and city
2. **Availability Check**: Uses OCR and DOM parsing to detect ticket availability
3. **Smart Detection**: Identifies when booking opens vs. when shows are not yet listed

### 2. Automated Booking Process

1. **Cinema Selection**: Chooses from your preferred cinemas
2. **Showtime Selection**: Picks showtimes matching your preferences
3. **Seat Selection**: Automatically selects your preferred seats
4. **Booking Initiation**: Proceeds to payment page
5. **Notification**: Sends email alerts about booking status

### 3. Security Features

- **Encrypted Credentials**: All sensitive data is encrypted using Fernet encryption
- **Secure Key Management**: Encryption keys stored separately
- **No Plain Text Storage**: Passwords and payment info never stored in plain text

## Troubleshooting

### Common Issues

1. **Chrome Driver Issues**:
   ```bash
   # Update Chrome to latest version
   # Reinstall undetected-chromedriver
   pip install --upgrade undetected-chromedriver
   ```

2. **Tesseract OCR Not Found**:
   - Ensure Tesseract is installed at `C:\Program Files\Tesseract-OCR\`
   - Add to PATH environment variable

3. **Email Notifications Not Working**:
   - Use Gmail App Passwords (not regular password)
   - Enable 2FA and generate app-specific password

4. **Booking Failures**:
   - Check if your credentials are correct
   - Verify preferred seats are available
   - Check cinema names match exactly

### Debug Mode

Run with verbose output to see detailed logs:

```bash
python booking_cli.py --verbose monitor
```

### Log Files

Check `booking_agent.log` for detailed execution logs.

## Safety and Legal Considerations

⚠️ **Important Notes:**

1. **Terms of Service**: Ensure your usage complies with BookMyShow's terms of service
2. **Rate Limiting**: The agent includes delays to avoid overwhelming servers
3. **Manual Oversight**: Always verify bookings and be prepared for manual intervention
4. **Backup Plan**: Have alternative booking methods ready
5. **Responsible Usage**: Don't use for scalping or commercial purposes

## File Structure

```
bmsEmailAlerts/
├── automated_booking_agent.py  # Main booking logic
├── booking_cli.py             # Command-line interface
├── credential_manager.py      # Secure credential management
├── monitor.py                # Original monitoring script
├── config.json               # Configuration file
├── requirements.txt          # Python dependencies
├── README.md                # This file
├── booking_agent.log         # Log file (created at runtime)
├── credentials.enc           # Encrypted credentials (created at setup)
└── secret.key               # Encryption key (created at setup)
```

## Contributing

Feel free to submit issues and enhancement requests!

## License

This project is for educational and personal use only. Use responsibly and in accordance with BookMyShow's terms of service.

---

**Happy Movie Booking! 🍿🎬**