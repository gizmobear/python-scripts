#!/usr/bin/env python3
"""
Windows Software Inventory Script
Performs a software inventory of traditionally installed applications on Windows,
excluding pre-requisites like .NET, C++ runtimes, etc.
"""

import sys
import platform
import winreg
import json
import csv
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Dict, Optional

# Keywords to filter out pre-requisites and system components
EXCLUDE_KEYWORDS = [
    'microsoft .net',
    '.net framework',
    'visual c++',
    'visual studio',
    'c++ redistributable',
    'microsoft visual c++',
    'windows sdk',
    'update for',
    'security update',
    'hotfix',
    'kb',
    'language pack',
    'driver',
    'runtime',
    'redistributable',
    'prerequisite',
    'prereq',
]


def check_windows_os() -> bool:
    """Check if the operating system is Windows."""
    if platform.system() != 'Windows':
        print("Error: This script is designed for Windows only.")
        print(f"Detected OS: {platform.system()}")
        return False
    return True


def get_installed_software() -> List[Dict[str, str]]:
    """
    Retrieve installed software from Windows Registry.
    Returns a list of dictionaries containing software information.
    """
    software_list = []
    registry_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    
    seen_names = set()  # To avoid duplicates
    
    for hkey, path in registry_paths:
        try:
            key = winreg.OpenKey(hkey, path)
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    
                    try:
                        display_name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        display_version = winreg.QueryValueEx(subkey, "DisplayVersion")[0] if winreg.QueryValueEx(subkey, "DisplayVersion")[0] else "N/A"
                    except (FileNotFoundError, OSError):
                        i += 1
                        continue
                    
                    # Skip if already seen or if it's a pre-requisite
                    if display_name.lower() in seen_names:
                        i += 1
                        continue
                    
                    # Check if it should be excluded
                    should_exclude = False
                    display_name_lower = display_name.lower()
                    for keyword in EXCLUDE_KEYWORDS:
                        if keyword in display_name_lower:
                            should_exclude = True
                            break
                    
                    if not should_exclude:
                        try:
                            publisher = winreg.QueryValueEx(subkey, "Publisher")[0]
                        except (FileNotFoundError, OSError):
                            publisher = "N/A"
                        
                        try:
                            install_date = winreg.QueryValueEx(subkey, "InstallDate")[0]
                        except (FileNotFoundError, OSError):
                            install_date = "N/A"
                        
                        software_list.append({
                            'name': display_name,
                            'version': display_version,
                            'publisher': publisher,
                            'install_date': install_date
                        })
                        seen_names.add(display_name_lower)
                    
                    winreg.CloseKey(subkey)
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except (FileNotFoundError, OSError):
            continue
    
    # Sort by name
    software_list.sort(key=lambda x: x['name'].lower())
    return software_list


def display_software_list(software_list: List[Dict[str, str]]) -> None:
    """Display the software list with formatting."""
    print("\n" + "=" * 80)
    print("INSTALLED SOFTWARE INVENTORY")
    print("=" * 80)
    print(f"{'Name':<50} {'Version':<20} {'Publisher':<30}")
    print("-" * 80)
    
    for software in software_list:
        name = software['name'][:47] + "..." if len(software['name']) > 50 else software['name']
        version = software['version'][:17] + "..." if len(software['version']) > 20 else software['version']
        publisher = software['publisher'][:27] + "..." if len(software['publisher']) > 30 else software['publisher']
        print(f"{name:<50} {version:<20} {publisher:<30}")
    
    print("-" * 80)
    print(f"Total Applications: {len(software_list)}")
    print("=" * 80 + "\n")


def export_to_csv(software_list: List[Dict[str, str]], filename: str) -> None:
    """Export software list to CSV format."""
    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['name', 'version', 'publisher', 'install_date']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(software_list)
    print(f"Software inventory exported to {filename}")


def export_to_text(software_list: List[Dict[str, str]], filename: str) -> None:
    """Export software list to plain text format."""
    with open(filename, 'w', encoding='utf-8') as f:
        f.write("INSTALLED SOFTWARE INVENTORY\n")
        f.write("=" * 80 + "\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 80 + "\n\n")
        
        for software in software_list:
            f.write(f"Name: {software['name']}\n")
            f.write(f"  Version: {software['version']}\n")
            f.write(f"  Publisher: {software['publisher']}\n")
            f.write(f"  Install Date: {software['install_date']}\n")
            f.write("-" * 80 + "\n")
        
        f.write(f"\nTotal Applications: {len(software_list)}\n")
    print(f"Software inventory exported to {filename}")


def export_to_json(software_list: List[Dict[str, str]], filename: str) -> None:
    """Export software list to JSON format."""
    output = {
        'generated': datetime.now().isoformat(),
        'total_count': len(software_list),
        'applications': software_list
    }
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Software inventory exported to {filename}")


def export_to_xml(software_list: List[Dict[str, str]], filename: str) -> None:
    """Export software list to XML format."""
    root = ET.Element('SoftwareInventory')
    root.set('generated', datetime.now().isoformat())
    root.set('total_count', str(len(software_list)))
    
    for software in software_list:
        app = ET.SubElement(root, 'Application')
        ET.SubElement(app, 'Name').text = software['name']
        ET.SubElement(app, 'Version').text = software['version']
        ET.SubElement(app, 'Publisher').text = software['publisher']
        ET.SubElement(app, 'InstallDate').text = software['install_date']
    
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(filename, encoding='utf-8', xml_declaration=True)
    print(f"Software inventory exported to {filename}")


def get_export_format() -> str:
    """Prompt user for export format with validation and looping."""
    valid_formats = ['csv', 'text', 'txt', 'json', 'xml']
    
    while True:
        response = input("Export format (csv, text, json, xml): ").strip().lower()
        
        if not response:
            print("Invalid response. Please enter a format: csv, text, json, or xml.")
            continue
        
        # Normalize the response
        if response in ['txt']:
            response = 'text'
        
        if response in valid_formats:
            return response
        else:
            print(f"Invalid response: '{response}'. Please enter one of: csv, text, json, or xml.")


def main():
    """Main function to run the software inventory."""
    # Check if running on Windows
    if not check_windows_os():
        sys.exit(1)
    
    print("Collecting installed software information...")
    software_list = get_installed_software()
    
    if not software_list:
        print("No software found or unable to access registry.")
        sys.exit(1)
    
    # Display the software list
    display_software_list(software_list)
    
    # Prompt for export
    export_format = get_export_format()
    
    # Generate filename with timestamp
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    format_extensions = {
        'csv': 'csv',
        'text': 'txt',
        'json': 'json',
        'xml': 'xml'
    }
    extension = format_extensions[export_format]
    filename = f"software_inventory_{timestamp}.{extension}"
    
    # Export based on format
    if export_format == 'csv':
        export_to_csv(software_list, filename)
    elif export_format == 'text':
        export_to_text(software_list, filename)
    elif export_format == 'json':
        export_to_json(software_list, filename)
    elif export_format == 'xml':
        export_to_xml(software_list, filename)


if __name__ == "__main__":
    main()

