#!/usr/bin/env python3
"""
Movie Booking Agent CLI
A comprehensive command-line interface for automated movie ticket booking
"""

import argparse
import sys
import json
from datetime import datetime, timedelta
from automated_booking_agent import MovieBookingAgent
from credential_manager import SecureCredentialManager
import logging

def setup_logging(verbose=False):
    """Setup logging configuration"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

def cmd_add_booking(agent, args):
    """Add a new booking request"""
    try:
        # Parse time range
        time_range = args.time_range.split(',') if args.time_range else ['evening']
        
        # Parse cinemas
        cinemas = args.cinemas.split(',') if args.cinemas else ['PVR Brookfields', 'INOX Prozone']
        
        req_id = agent.add_booking_request(
            movie_name=args.movie,
            date=args.date,
            time_range=time_range,
            cinemas=cinemas,
            city=args.city,
            max_price=args.max_price,
            auto_book=args.auto_book
        )
        
        print(f"✅ Added booking request: {req_id}")
        print(f"   Movie: {args.movie}")
        print(f"   Date: {args.date}")
        print(f"   Time Range: {', '.join(time_range)}")
        print(f"   Cinemas: {', '.join(cinemas)}")
        print(f"   Auto-book: {'Yes' if args.auto_book else 'No'}")
        
    except Exception as e:
        print(f"❌ Error adding booking: {e}")
        return False
    
    return True

def cmd_list_bookings(agent, args):
    """List all booking requests"""
    if not agent.booking_requests:
        print("No booking requests found.")
        return True
    
    print("\n📋 Booking Requests:")
    print("-" * 80)
    
    for i, req in enumerate(agent.booking_requests, 1):
        status_emoji = {
            'monitoring': '👀',
            'booking_initiated': '🎫',
            'completed': '✅',
            'failed': '❌',
            'expired': '⏰'
        }.get(req.status, '❓')
        
        print(f"{i}. {status_emoji} [{req.id}] {req.movie_name}")
        print(f"   Date: {req.date} | Status: {req.status}")
        print(f"   Cinemas: {', '.join(req.cinemas)}")
        print(f"   Time Range: {', '.join(req.preferred_time_range)}")
        if args.verbose:
            print(f"   Max Price: ₹{req.max_price} | Auto-book: {req.auto_book}")
            print(f"   Created: {req.created_at}")
        print()
    
    return True

def cmd_remove_booking(agent, args):
    """Remove a booking request"""
    try:
        # Find booking by ID
        booking_to_remove = None
        for booking in agent.booking_requests:
            if booking.id == args.booking_id:
                booking_to_remove = booking
                break
        
        if not booking_to_remove:
            print(f"❌ Booking with ID {args.booking_id} not found.")
            return False
        
        # Remove from list
        agent.booking_requests.remove(booking_to_remove)
        
        # Remove from config
        agent.config['booking_requests'] = [
            req for req in agent.config['booking_requests'] 
            if req['id'] != args.booking_id
        ]
        agent.save_config()
        
        print(f"✅ Removed booking request: {args.booking_id}")
        return True
        
    except Exception as e:
        print(f"❌ Error removing booking: {e}")
        return False

def cmd_monitor(agent, args):
    """Start monitoring bookings"""
    try:
        print("🔄 Starting monitoring...")
        print("Press Ctrl+C to stop monitoring")
        
        monitor_thread = agent.start_monitoring()
        
        # Keep main thread alive
        try:
            while monitor_thread.is_alive():
                monitor_thread.join(1)
        except KeyboardInterrupt:
            print("\n⏹️  Monitoring stopped by user.")
            
    except Exception as e:
        print(f"❌ Error during monitoring: {e}")
        return False
    
    return True

def cmd_check_single(agent, args):
    """Check availability for a single movie/date"""
    try:
        print(f"🔍 Checking availability for {args.movie} on {args.date}...")
        
        agent.setup_driver()
        
        # Create a temporary booking request
        from automated_booking_agent import BookingRequest
        temp_request = BookingRequest(
            id="temp_check",
            movie_name=args.movie,
            date=args.date,
            preferred_time_range=['evening'],
            cinemas=['PVR', 'INOX'],
            city=args.city,
            max_price=300,
            priority=1,
            auto_book=False,
            status='checking',
            created_at=datetime.now().isoformat()
        )
        
        # Find movie URL
        movie_url = agent.find_movie_url(args.movie, args.date, args.city)
        
        if not movie_url:
            print(f"❌ Could not find booking URL for {args.movie}")
            return False
        
        print(f"🔗 Found URL: {movie_url}")
        
        # Check availability
        available = agent.check_availability(movie_url)
        
        if available:
            print(f"✅ Tickets are available for {args.movie}!")
        else:
            print(f"❌ No tickets available yet for {args.movie}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error checking availability: {e}")
        return False
    finally:
        if agent.driver:
            agent.driver.quit()

def cmd_setup_credentials(agent, args):
    """Setup user credentials"""
    try:
        manager = SecureCredentialManager()
        success = manager.store_credentials()
        
        if success:
            print("✅ Credentials setup completed!")
        else:
            print("❌ Failed to setup credentials.")
            
        return success
        
    except Exception as e:
        print(f"❌ Error setting up credentials: {e}")
        return False

def cmd_config(agent, args):
    """Show or update configuration"""
    if args.show:
        print("📋 Current Configuration:")
        print(json.dumps(agent.config, indent=2))
        return True
    
    if args.set:
        try:
            key, value = args.set.split('=', 1)
            
            # Navigate nested config
            keys = key.split('.')
            config_ref = agent.config
            
            for k in keys[:-1]:
                if k not in config_ref:
                    config_ref[k] = {}
                config_ref = config_ref[k]
            
            # Try to parse value as JSON, fallback to string
            try:
                config_ref[keys[-1]] = json.loads(value)
            except:
                config_ref[keys[-1]] = value
            
            agent.save_config()
            print(f"✅ Updated {key} = {value}")
            return True
            
        except Exception as e:
            print(f"❌ Error updating config: {e}")
            return False
    
    return True

def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description='Movie Booking Agent - Automated ticket booking for BookMyShow',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s add "Coolie" "2025-08-15" --time-range "evening" --cinemas "PVR Brookfields,INOX Prozone"
  %(prog)s list --verbose
  %(prog)s monitor
  %(prog)s check "Coolie" "2025-08-15" --city "Coimbatore"
  %(prog)s setup-credentials
        """
    )
    
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    parser.add_argument('--config', '-c', default='config.json', help='Config file path')
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Add booking command
    add_parser = subparsers.add_parser('add', help='Add a new booking request')
    add_parser.add_argument('movie', help='Movie name')
    add_parser.add_argument('date', help='Show date (YYYY-MM-DD)')
    add_parser.add_argument('--time-range', default='evening', 
                           help='Preferred time range (comma-separated: morning,afternoon,evening,night)')
    add_parser.add_argument('--cinemas', default='PVR Brookfields,INOX Prozone',
                           help='Preferred cinemas (comma-separated)')
    add_parser.add_argument('--city', default='Coimbatore', help='City name')
    add_parser.add_argument('--max-price', type=float, default=300.0, help='Maximum ticket price')
    add_parser.add_argument('--auto-book', action='store_true', default=True, 
                           help='Enable automatic booking')
    add_parser.add_argument('--no-auto-book', dest='auto_book', action='store_false',
                           help='Disable automatic booking (notification only)')
    
    # List bookings command
    list_parser = subparsers.add_parser('list', help='List all booking requests')
    list_parser.add_argument('--verbose', action='store_true', help='Show detailed information')
    
    # Remove booking command
    remove_parser = subparsers.add_parser('remove', help='Remove a booking request')
    remove_parser.add_argument('booking_id', help='Booking request ID to remove')
    
    # Monitor command
    monitor_parser = subparsers.add_parser('monitor', help='Start monitoring all active bookings')
    
    # Check single availability
    check_parser = subparsers.add_parser('check', help='Check availability for a specific movie')
    check_parser.add_argument('movie', help='Movie name to check')
    check_parser.add_argument('date', help='Date to check (YYYY-MM-DD)')
    check_parser.add_argument('--city', default='Coimbatore', help='City name')
    
    # Setup credentials
    setup_parser = subparsers.add_parser('setup-credentials', help='Setup BookMyShow credentials')
    
    # Config management
    config_parser = subparsers.add_parser('config', help='Manage configuration')
    config_parser.add_argument('--show', action='store_true', help='Show current configuration')
    config_parser.add_argument('--set', help='Set config value (key=value)')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    # Setup logging
    setup_logging(args.verbose)
    
    try:
        # Initialize agent
        agent = MovieBookingAgent(args.config)
        
        # Command routing
        commands = {
            'add': cmd_add_booking,
            'list': cmd_list_bookings,
            'remove': cmd_remove_booking,
            'monitor': cmd_monitor,
            'check': cmd_check_single,
            'setup-credentials': cmd_setup_credentials,
            'config': cmd_config
        }
        
        command_func = commands.get(args.command)
        if command_func:
            success = command_func(agent, args)
            return 0 if success else 1
        else:
            print(f"Unknown command: {args.command}")
            return 1
            
    except KeyboardInterrupt:
        print("\n⏹️  Interrupted by user.")
        return 1
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())