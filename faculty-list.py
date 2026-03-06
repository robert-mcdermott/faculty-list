import argparse
import sys
import csv
import requests
import json
import time
from urllib.parse import urljoin
from pathlib import Path
from bs4 import BeautifulSoup
import re


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Create CSV inventory of Fred Hutchinson faculty profiles"
    )
    
    # Required arguments
    parser.add_argument(
        "url_file",
        help="Text file containing faculty URLs (one per line)"
    )
    parser.add_argument(
        "output_csv",
        help="Output CSV file name"
    )
    
    # Optional arguments
    parser.add_argument(
        "--endpoint",
        default="http://localhost:11434/v1/chat/completions",
        help="OpenAI-compatible API endpoint URL (default: %(default)s)"
    )
    parser.add_argument(
        "--model",
        default="qwen2.5:3b",
        help="LLM model name (default: %(default)s)"
    )
    parser.add_argument(
        "--api-key",
        default="sk-1234",
        help="API key for the LLM endpoint (default: %(default)s)"
    )
    
    return parser.parse_args()


def load_urls(url_file):
    """Load URLs from text file, one per line."""
    try:
        with open(url_file, 'r', encoding='utf-8') as f:
            urls = [line.strip().lstrip('\ufeff') for line in f if line.strip()]
        return urls
    except FileNotFoundError:
        print(f"Error: URL file '{url_file}' not found")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading URL file '{url_file}': {e}")
        sys.exit(1)


def fetch_page_content(url):
    """Fetch content from a faculty profile URL."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        return None


def parse_faculty_page(html_content):
    """Parse HTML content to extract structured information."""
    soup = BeautifulSoup(html_content, 'lxml')
    
    # Extract main content sections
    sections = {}
    
    # Get the main faculty information
    faculty_info = {}
    
    # Try to find the main content area
    main_content = soup  # Default to full soup
    
    # Try to find a better container that has the faculty content
    test_keywords = ["Research Interests", "Clinical Expertise", "Education", "Degrees"]
    for container in [soup.body, soup.find('div', class_=re.compile('container', re.I)), soup]:
        if container:
            container_text = container.get_text()
            if any(keyword in container_text for keyword in test_keywords):
                main_content = container
                break
    
    # Extract name (usually in h1)
    name_elem = main_content.find('h1')
    if name_elem:
        faculty_info['name'] = name_elem.get_text(strip=True)
    
    # Look for contact information sections
    contact_section_content = ""
    
    # Find contact/biographical information
    for section_elem in main_content.find_all(['div', 'section'], class_=re.compile('contact|bio|info', re.I)):
        if section_elem:
            contact_section_content += section_elem.get_text(separator='\n', strip=True) + "\n\n"
    
    sections['contact_section'] = contact_section_content
    
    # Look for Research/Clinical sections
    research_section_content = ""
    
    # Find research interests, clinical expertise sections
    for heading in main_content.find_all(['h2', 'h3', 'h4', 'h5', 'h6']):
        heading_text = heading.get_text().lower()
        if any(keyword in heading_text for keyword in ['research', 'clinical', 'expertise', 'interests', 'studies']):
            # Get content following this heading
            current = heading.next_sibling
            section_elements = []
            
            while current:
                if hasattr(current, 'name'):
                    if current.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                        # Stop when we hit another major heading
                        break
                    elif current.name in ['p', 'div', 'li', 'ul']:
                        text = current.get_text(separator=' ', strip=True)
                        if text and len(text) > 3:
                            section_elements.append(text)
                elif hasattr(current, 'strip'):
                    text = current.strip()
                    if text and len(text) > 3:
                        section_elements.append(text)
                current = current.next_sibling
            
            if section_elements:
                research_section_content += f"=== {heading.get_text(strip=True)} ===\n"
                research_section_content += '\n'.join(section_elements) + "\n\n"
    
    sections['research_section'] = research_section_content
    
    # Get all text content for fallback
    sections['full_text'] = main_content.get_text(separator='\n', strip=True)

    # Look for last modified date in meta tags
    last_modified = ""
    meta_modified = soup.find('meta', attrs={'name': 'last_modified'})
    if meta_modified and meta_modified.get('content'):
        raw = meta_modified['content']
        # Parse format like "20250623T172551.824-0700" into "June 23, 2025"
        match = re.match(r'(\d{4})(\d{2})(\d{2})T', raw)
        if match:
            from datetime import date
            d = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            last_modified = d.strftime('%B %d, %Y')

    # Fallback: look for "Last Modified" text in page content
    if not last_modified:
        full_text = soup.get_text()
        patterns = [
            r'last modified[,:\s]+([A-Za-z]+ \d{1,2}, \d{4})',
            r'last updated[,:\s]+([A-Za-z]+ \d{1,2}, \d{4})',
            r'modified[,:\s]+([A-Za-z]+ \d{1,2}, \d{4})',
            r'updated[,:\s]+([A-Za-z]+ \d{1,2}, \d{4})',
        ]
        for pattern in patterns:
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                last_modified = match.group(1)
                break

    return sections, faculty_info, last_modified


def extract_faculty_data(content, url, api_endpoint, model, api_key):
    """Extract structured data from faculty profile using LLM API."""
    
    # Parse the HTML content first to get structured sections
    sections, faculty_info, last_modified = parse_faculty_page(content)
    
    # Build focused content for the LLM
    focused_content = ""
    
    # Include contact section if found
    if sections.get('contact_section') and len(sections['contact_section']) > 50:
        focused_content += "=== CONTACT/BIOGRAPHICAL SECTION ===\n"
        focused_content += sections['contact_section'] + "\n\n"
    
    # Include research section if found
    if sections.get('research_section') and len(sections['research_section']) > 50:
        focused_content += "=== RESEARCH/CLINICAL SECTIONS ===\n"
        focused_content += sections['research_section'] + "\n\n"
    
    # Always include full content as fallback
    focused_content += "=== FULL PAGE CONTENT ===\n"
    content_limit = 15000  # Large enough to capture all sections
    focused_content += sections['full_text'][:content_limit]
    
    prompt = f"""
You are extracting information from a Fred Hutchinson Cancer Center faculty profile page. Please extract ALL the following information and return it as valid JSON:

{{
  "Name": "Full name of the faculty member",
  "Degrees": "Academic degrees (MD, PhD, etc.) - combine all degrees into single field",
  "Titles": "Academic and professional titles and positions at Fred Hutch and affiliations",
  "Phone": "Phone number",
  "Email": "Email address", 
  "Fax": "Fax number",
  "Mail Stop": "Mail stop code/address",
  "Other Appointments & Affiliations": "Other academic appointments, affiliations, professorships",
  "Education": "Educational background including institutions, degrees, and years",
  "Research Interests": "Research focus areas and interests",
  "Clinical Expertise": "Clinical specialties and expertise areas",
  "Current Studies": "Current research studies and projects"
}}

CRITICAL INSTRUCTIONS:
1. EXTRACT ALL FIELDS - Search the ENTIRE content thoroughly
2. For "Degrees" - Extract all academic degrees (MD, PhD, DVM, etc.) and combine them
3. For "Titles" - Include all professional titles, positions, division affiliations
4. For contact info - Look for phone, email, fax, mail stop in contact sections
5. For "Other Appointments & Affiliations" - Include professorships, endowed chairs, adjunct positions
6. For "Education" - Include undergraduate, graduate, medical school, postdoc details with institutions and years
7. For "Research Interests" - Look for research focus areas, keywords, specializations
8. For "Clinical Expertise" - Find clinical specialties, diseases treated, patient care areas
9. For "Current Studies" - Look for ongoing research projects, studies, investigations
10. Return ONLY valid JSON - no extra text or explanation
11. Use empty string "" ONLY if information truly cannot be found after thorough search
12. Pay attention to section headings and structured content areas

Faculty profile content:
{focused_content}
"""
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.1
    }
    
    try:
        response = requests.post(api_endpoint, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        llm_content = result["choices"][0]["message"]["content"]
        
        # Try to parse JSON from the response
        try:
            # Look for JSON in the response
            start_idx = llm_content.find('{')
            end_idx = llm_content.rfind('}') + 1
            if start_idx >= 0 and end_idx > start_idx:
                json_str = llm_content[start_idx:end_idx]
                data = json.loads(json_str)
                
                # Add the profile URL and last modified date
                data["URL"] = url
                data["Last Modified"] = last_modified

                return data
            else:
                return None
        except json.JSONDecodeError:
            return None
            
    except requests.exceptions.RequestException:
        return None
    except Exception:
        return None


def write_csv_header(output_file):
    """Write CSV header row."""
    fieldnames = [
        "Name", "Degrees", "Titles", "Phone", "Email", "Fax", "Mail Stop",
        "Other Appointments & Affiliations", "Education", "Research Interests",
        "Clinical Expertise", "Current Studies", "URL", "Last Modified"
    ]
    
    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
    
    return fieldnames


def append_to_csv(output_file, data, fieldnames):
    """Append a row to the CSV file."""
    with open(output_file, 'a', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        # Ensure all fields are present
        row = {field: data.get(field, "") for field in fieldnames}
        writer.writerow(row)


def print_progress(current, total, url, success):
    """Print progress information."""
    percentage = (current / total) * 100
    status = "✓" if success else "✗"
    print(f"[{current:3d}/{total:3d}] ({percentage:5.1f}%) {status} {url}")


def main():
    args = parse_arguments()
    
    # Load URLs
    print(f"Loading URLs from {args.url_file}...")
    urls = load_urls(args.url_file)
    print(f"Found {len(urls)} URLs to process")
    
    # Initialize CSV file
    print(f"Initializing output CSV: {args.output_csv}")
    fieldnames = write_csv_header(args.output_csv)
    
    # Process URLs
    successful = 0
    failed_urls = []
    
    print("\nProcessing faculty profiles...")
    print("=" * 70)
    
    for i, url in enumerate(urls, 1):
        # Fetch page content
        content = fetch_page_content(url)
        
        if content is None:
            failed_urls.append(url)
            print_progress(i, len(urls), url, False)
            continue
        
        # Extract data using LLM
        faculty_data = extract_faculty_data(content, url, args.endpoint, args.model, args.api_key)
        
        if faculty_data is None:
            failed_urls.append(url)
            print_progress(i, len(urls), url, False)
            continue
        
        # Write to CSV
        append_to_csv(args.output_csv, faculty_data, fieldnames)
        successful += 1
        print_progress(i, len(urls), url, True)
        
        # Small delay
        time.sleep(0.5)
    
    # Print final statistics
    print("\n" + "=" * 70)
    print("PROCESSING COMPLETE")
    print("=" * 70)
    print(f"Total URLs processed: {len(urls)}")
    print(f"Successful extractions: {successful}")
    print(f"Failed extractions: {len(failed_urls)}")
    print(f"Success rate: {(successful/len(urls)*100):.1f}%")
    print(f"Output written to: {args.output_csv}")
    
    if failed_urls:
        print("\nFailed URLs:")
        for url in failed_urls:
            print(f"  - {url}")


if __name__ == "__main__":
    main()
