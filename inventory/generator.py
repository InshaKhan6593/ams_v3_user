from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from io import BytesIO
import os

class InspectionCertificateGenerator:
    def __init__(self, logo_path=None, data={}, item_data={}, rejected_item_data={}):
        self.logo_path = logo_path
        self.width, self.height = letter
        self.styles = getSampleStyleSheet()
        self.BOTTOM_MARGIN = 50  # Increased bottom margin
        
        # Ensure all data has default values
        self.data = data or {}
        self.item_data = item_data or {
            'descriptions': [], 'acct_unit': [], 't_quantity': [], 'r_quantity': [], 'a_quantity': []
        }
        self.rejected_item_data = rejected_item_data or {'item_no': [], 'reasons': []}
        
        print(f"Generator initialized - Items: {len(self.item_data['descriptions'])}, Rejected: {len(self.rejected_item_data['item_no'])}")
        
        # Create buffer
        self.buffer = BytesIO()
        try:
            c = canvas.Canvas(self.buffer, pagesize=letter)
            self.generate_form(c)
            c.save()
            self.buffer.seek(0)
            print("PDF generation completed successfully")
        except Exception as e:
            print(f"Error during PDF generation: {str(e)}")
            raise
        
    def get_pdf(self):
        """Return the PDF buffer"""
        return self.buffer
        
    def generate_form(self, c):
        try:
            self.page_header(c)
            self.drawLogo(c)
            self.drawIndenterSection(c)
            last_y = self.draw_item_table(c)
            last_y = self.drawConsigneeSection(c, last_y)
            last_y = self.drawCentralStoreSection(c, last_y)
            self.drawFinanceSection(c, last_y)
        except Exception as e:
            print(f"Error in generate_form: {str(e)}")
            raise
    
    def check_space_and_new_page(self, c, current_y, required_space):
        """Check if there's enough space, create new page if needed"""
        if current_y - required_space < self.BOTTOM_MARGIN:
            c.showPage()
            self.page_header(c)
            return self.height - 50
        return current_y
    
    def page_header(self, c):
        c.setFont("Helvetica-Bold", 10)
        c.drawString(self.width - 163, self.height - 17, "F/QSP 10/06/01")
        
    def page_footer(self, c):
        c.setFont("Helvetica-Bold", 12) 
        c.drawString(self.width - 117, self.height - 750, "P.T.O")
    
    def drawLogo(self, c):
        if self.logo_path and os.path.exists(self.logo_path):
            try:
                c.drawImage(self.logo_path, x=45, y=self.height - 100, width=inch, height=inch,
                            preserveAspectRatio=True, mask='auto')
            except:
                print("Logo not found or cannot be drawn")
        
    def drawIndenterSection(self, c):
        c.setFont("Helvetica-Bold", 10)
        c.drawString(self.width - 163, self.height - 31.5, "Issued On: ______________")
        c.drawString(self.width - 163, self.height - 45.5, "Issued To: _______________")

        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(self.width / 2, self.height - 68, "NED UNIVERSITY OF ENGINEERING & TECHNOLOGY, KARACHI")
        c.drawCentredString(self.width / 2, self.height - 88, "PURCHASE SECTION")

        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(self.width / 2, self.height - 109, "INSPECTION CERTIFICATE")

        text_width = c.stringWidth("INSPECTION CERTIFICATE", "Helvetica-Bold", 14)
        c.setLineWidth(1)
        c.line((self.width - text_width) / 2, self.height - 111, (self.width + text_width) / 2, self.height - 111)

        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(self.width / 2, self.height - 133, "(To be completed by Consignee / Indenter) ")

        c.setFont("Helvetica", 11)
        
        # Contract No and Date
        c.drawCentredString(self.width / 2, self.height - 152,
                            "1. Contract No: _______________________________________________ Date:________________")
        c.drawString(120, self.height - 152, f"{self.data.get('contract_no', 'N/A')}")
        c.drawString(self.width - 150, self.height - 152, f"{self.data.get('date', '')}")
        
        # Contractor's Name and address
        c.drawCentredString(self.width / 2, self.height - 170,
                            "2. Contractor's Name and address: ____________________________________________________")
        contractor_text = f"{self.data.get('contractor_name', 'N/A')}, {self.data.get('contractor_address', 'N/A')}"
        c.drawString(120, self.height - 170, contractor_text[:80] + "..." if len(contractor_text) > 80 else contractor_text)
        
        # Indenter and Indent No
        c.drawCentredString(self.width / 2, self.height - 188,
                            "3. Indenter. ________________________________ 4. Indent No ____________________________")
        c.drawString(120, self.height - 188, f"{self.data.get('indenter', 'N/A')}")
        c.drawString(self.width - 220, self.height - 188, f"{self.data.get('indent_no', 'N/A')}")
        
        # Consignee and Department
        c.drawCentredString(self.width / 2, self.height - 206,
                            "5. Consignee: ________________________________ 6. Department: ________________________")
        c.drawString(120, self.height - 206, f"{self.data.get('consignee', 'N/A')}")
        c.drawString(self.width - 200, self.height - 206, f"{self.data.get('department', 'N/A')}")
        
        # Date of Delivery and Delivery status
        c.drawCentredString(self.width / 2, self.height - 224,
                            "7. Date of Delivery. ____________________________ 8. Delivery in part or full _________________")
        c.drawString(120, self.height - 224, f"{self.data.get('date_of_delivery', '')}")
        c.drawString(self.width - 160, self.height - 224, f"{self.data.get('delivery_status', 'FULL')}")

        c.drawString(50, self.height - 242, "9. Details of Stores delivered.")
    
    def draw_item_table(self, c):
        styles = getSampleStyleSheet()
        normal = styles["Normal"]

        # Create table data
        all_data = [
            ["Item No.", "DESCRIPTION OF STORES", "Acct. Unit",
            "Tendered\n(Quantity)", "Rejected\n(Quantity)", "Accepted\n(Quantity)"]
        ]

        # Add items to table
        for i in range(len(self.item_data['descriptions'])):
            all_data.append([
                str(i+1),
                Paragraph(self.item_data['descriptions'][i], normal),
                self.item_data['acct_unit'][i] if i < len(self.item_data['acct_unit']) else 'N/A',
                self.item_data['t_quantity'][i] if i < len(self.item_data['t_quantity']) else '0',
                self.item_data['r_quantity'][i] if i < len(self.item_data['r_quantity']) else '0',
                self.item_data['a_quantity'][i] if i < len(self.item_data['a_quantity']) else '0'
            ])

        table_style = TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (2, 1), (-1, -1), "CENTER"),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ])

        x_margin = 40
        y_start = self.height - 260
        bottom_margin = 50

        if not all_data or len(all_data) <= 1:
            # No items to display
            c.setFont("Helvetica", 10)
            c.drawString(x_margin, y_start, "No items in this inspection certificate.")
            return y_start - 30

        header = all_data[0]
        rows = all_data[1:]
        current_y = y_start
        chunk = [header]

        for row in rows:
            temp_table = Table(chunk + [row],
                        colWidths=[0.7*inch, 3*inch, 0.8*inch, 1*inch, 1*inch, 1*inch])
            temp_table.setStyle(table_style)
            _, h = temp_table.wrap(self.width, self.height)
            
            if current_y - h < bottom_margin:  
                # Draw current chunk and start new page
                temp_table = Table(chunk, colWidths=[0.7*inch, 3*inch, 0.8*inch, 1*inch, 1*inch, 1*inch])
                temp_table.setStyle(table_style)
                _, h_chunk = temp_table.wrap(self.width, self.height)
                temp_table.drawOn(c, x_margin, current_y - h_chunk)

                c.showPage()
                self.page_header(c)
                current_y = self.height - 50
                chunk = [header, row]
            else:
                chunk.append(row)

        # Draw remaining chunk
        if len(chunk) > 1:  # More than just header
            temp_table = Table(chunk, colWidths=[0.7*inch, 3*inch, 0.8*inch, 1*inch, 1*inch, 1*inch])
            temp_table.setStyle(table_style)
            _, h_chunk = temp_table.wrap(self.width, self.height)
            temp_table.drawOn(c, x_margin, current_y - h_chunk)
            current_y = current_y - h_chunk

        c.showPage()
        self.page_header(c)
        return self.height - 50

    def drawConsigneeSection(self, c, last_y):
        last_y = self.check_space_and_new_page(c, last_y, 74)
        
        c.setFont("Helvetica-Bold", 12)
        c.drawString(50, last_y - 20, "10. Consignee / Indentor:")
        
        last_y = self.check_space_and_new_page(c, last_y - 20, 15)
        c.setFont("Helvetica", 12)
        c.drawString(70, last_y - 15, "a) Date of Inspection _______________")
        c.drawString(200, last_y - 15, f"{self.data.get('date_of_inspection', '')}")
        
        last_y = self.check_space_and_new_page(c, last_y - 15, 54)
        c.setFont("Helvetica", 11)
        c.drawString(70, last_y - 13, "b) Certified that the stores as mentioned on page 1 (para 9) have been received in good condition and")
        c.drawString(85, last_y - 26, "according to specifications as given in the Contract Order; except the following for the reasons as")
        c.drawString(85, last_y - 39, "below:")
        
        last_y = self.draw_rejection_table(c, last_y - 39)
        
        last_y = self.check_space_and_new_page(c, last_y, 54)
        
        c.setFont("Helvetica", 11)
        c.drawString(50, last_y, "c) The stores received have been entered in Stock Register No. _____________________________________")
        stock_registers = self.data.get('stock_register_no', [])
        gap = 0
        for i, reg_no in enumerate(stock_registers):
            if i < 3:  # Limit display to 3 registers
                c.drawString(350 + gap, last_y, f"{reg_no}")
                gap += 80
        
        c.drawString(50, last_y - 18, "Page No(s). ___________")
        c.drawString(50, last_y - 36, "d) Date of Entry: ___________________________")
        
        last_y = self.check_space_and_new_page(c, last_y - 36, 100)
        
        c.drawString(self.width - 242, last_y - 36, "Consignee's Signature _____________")
        c.drawString(self.width - 242, last_y - 54, "Name ___________________________")
        c.drawString(self.width - 242, last_y - 72, "Designation ___________________")
        
        c.drawString(50, last_y - 102, "___________________ ")
        c.drawString(50, last_y - 120, "Countersignature by")
        c.drawString(50, last_y - 134, "Chairman / Head of the Department")
        c.drawString(50, last_y - 148, "(if other than Consignee) ")
        
        c.setLineWidth(3)
        c.line(50, last_y - 164, self.width - 30, last_y - 164)
        
        return last_y - 164
        
    def drawCentralStoreSection(self, c, last_y):
        last_y = self.check_space_and_new_page(c, last_y, 180)
        
        c.setFont("Helvetica-Bold", 11)
        c.drawString(50, last_y - 14, "11. Central Store:")
        
        c.setFont("Helvetica", 11)
        c.drawString(70, last_y - 34, "a) The items have been registered in the Central Dead Stock Register (Non Stock) No:____________")
        c.drawString(self.width - 120, last_y - 34, f"{self.data.get('dead_stock_register_no', '')}")
        
        c.drawString(85, last_y - 54, "Page No(s). ______________________________________________________________________")
        c.drawString(70, last_y - 74, "b) Date of Entry:_________________")
        
        c.setFont("Helvetica-Bold", 11)
        c.drawString(self.width - 189, last_y - 120, "____________________")
        c.drawString(self.width - 189, last_y - 134, "Manager Central Store")

        c.setLineWidth(3)
        c.line(50, last_y - 150, self.width - 30, last_y - 150)
        
        return last_y - 150
    
    def drawFinanceSection(self, c, last_y):
        last_y = self.check_space_and_new_page(c, last_y, 110)
        
        c.setFont("Helvetica-Bold", 12)
        c.drawString(50, last_y - 20, "12. Purchase Section, Directorate of Finance:")
        
        c.setFont("Helvetica", 12)
        c.drawString(70, last_y - 36, "Checked and found all formalities of inspection have been completed.")
        
        c.setFont("Helvetica", 12)
        c.drawString(50, last_y - 97, "Dated: ______________")
        
        c.setFont("Helvetica-Bold", 12)
        c.drawString(self.width - 200, last_y - 97, "Assistant Director Finance")
        c.drawString(self.width - 160, last_y - 110, "(Purchase)")
       
    def draw_rejection_table(self, c, last_y, max_width=500):
        """Draws the rejection table with proper pagination"""
        styles = getSampleStyleSheet()
        normal = styles["Normal"]

        all_data = [["ITEM No", "REASONS FOR REJECTION"]]
        
        # Add rejected items
        for i in range(len(self.rejected_item_data['item_no'])):
            all_data.append([
                str(self.rejected_item_data['item_no'][i]), 
                Paragraph(self.rejected_item_data['reasons'][i], normal)
            ])

        if len(all_data) <= 1:
            # No rejected items
            c.setFont("Helvetica", 10)
            c.drawString(85, last_y - 20, "No items rejected.")
            return last_y - 40

        table_style = TableStyle([
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("ALIGN", (0, 1), (0, -1), "CENTER"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
        ])

        header = all_data[0]
        rows = all_data[1:]
        current_y = last_y
        chunk = [header]

        for row in rows:
            temp_table = Table(chunk + [row], colWidths=[1*inch, max_width - 45])
            temp_table.setStyle(table_style)
            _, h = temp_table.wrap(max_width, self.height)
            
            if current_y - h < self.BOTTOM_MARGIN:
                temp_table = Table(chunk, colWidths=[1*inch, max_width - 45])
                temp_table.setStyle(table_style)
                _, h_chunk = temp_table.wrap(max_width, self.height)
                temp_table.drawOn(c, 85, current_y - h_chunk)

                c.showPage()
                self.page_header(c)
                current_y = self.height - 50
                chunk = [header, row]
            else:
                chunk.append(row)

        if chunk:
            temp_table = Table(chunk, colWidths=[1*inch, max_width - 45])
            temp_table.setStyle(table_style)
            _, h_chunk = temp_table.wrap(max_width, self.height)
            temp_table.drawOn(c, 85, current_y - h_chunk)
            current_y = current_y - h_chunk

        return current_y - 20