import os
import tkinter as tk
from tkinter import scrolledtext, filedialog, messagebox
from assistant import DeepSeekDevAssistant  # استيراد الكلاس السابق

class CodeAssistantGUI:
    def __init__(self, master, api_key):
        self.master = master
        master.title("DeepSeek Code Assistant")
        
        # تهيئة المساعد
        self.assistant = DeepSeekDevAssistant(api_key)
        self.current_file = None
        
        # إنشاء واجهة المستخدم
        self.create_widgets()
        
        # إعداد الألوان والخطوط
        self.configure_styles()
        
    def create_widgets(self):
        # منطقة عرض المحادثة
        self.chat_history = scrolledtext.ScrolledText(
            self.master, 
            wrap=tk.WORD,
            state='disabled',
            height=20,
            width=80
        )
        self.chat_history.grid(row=0, column=0, columnspan=3, padx=10, pady=10)
        
        # منطقة الإدخال
        self.input_entry = tk.Entry(self.master, width=70)
        self.input_entry.grid(row=1, column=0, padx=10, pady=10)
        self.input_entry.bind("<Return>", lambda event: self.process_input())
        
        # أزرار التحكم
        self.send_btn = tk.Button(self.master, text="إرسال", command=self.process_input)
        self.send_btn.grid(row=1, column=1, padx=5)
        
        self.load_btn = tk.Button(self.master, text="تحميل ملف", command=self.load_file)
        self.load_btn.grid(row=1, column=2, padx=5)
        
        # منطقة عرض الكود
        self.code_display = scrolledtext.ScrolledText(
            self.master,
            wrap=tk.NONE,
            state='normal',
            height=15,
            width=80
        )
        self.code_display.grid(row=2, column=0, columnspan=3, padx=10, pady=10)
        
        # أزرار خاصة بالكود
        self.save_btn = tk.Button(self.master, text="حفظ التعديلات", command=self.save_file)
        self.save_btn.grid(row=3, column=0, pady=10)
        
        self.analyze_btn = tk.Button(self.master, text="تحليل الكود", command=self.analyze_code)
        self.analyze_btn.grid(row=3, column=1, pady=10)
        
        self.clear_btn = tk.Button(self.master, text="مسح الشاشة", command=self.clear_screen)
        self.clear_btn.grid(row=3, column=2, pady=10)
        
    def configure_styles(self):
        # تخصيص المظهر
        self.master.configure(bg='#f0f0f0')
        self.chat_history.configure(bg='#ffffff', fg='#333333')
        self.code_display.configure(bg='#f8f8f8', fg='#006600')
        self.input_entry.configure(bg='#ffffff')
        
    def update_chat(self, sender, message):
        # تحديث سجل المحادثة
        self.chat_history.configure(state='normal')
        self.chat_history.insert(tk.END, f"{sender}:\n{message}\n\n")
        self.chat_history.configure(state='disabled')
        self.chat_history.yview(tk.END)
        
    def process_input(self):
        # معالجة المدخلات
        user_input = self.input_entry.get()
        self.input_entry.delete(0, tk.END)
        
        if user_input.startswith('/'):
            self.handle_command(user_input)
        else:
            self.update_chat("أنت", user_input)
            self.process_assistant_response(user_input)
            
    def handle_command(self, command):
        # معالجة الأوامر الخاصة
        if command == '/load':
            self.load_file()
        elif command == '/save':
            self.save_file()
        elif command == '/clear':
            self.clear_screen()
        else:
            messagebox.showwarning("تحذير", "أمر غير معروف")
            
    def load_file(self):
        # تحميل ملف كود
        file_path = filedialog.askopenfilename(
            filetypes=[
                ("ملفات الكود", "*.py *.js *.html *.css"),
                ("كل الملفات", "*.*")
            ]
        )
        
        if file_path:
            result = self.assistant.analyze_code(file_path)
            self.current_file = file_path
            self.update_code_display(file_path)
            messagebox.showinfo("تم التحميل", result)
            
    def update_code_display(self, file_path):
        # عرض محتوى الملف
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            self.code_display.delete(1.0, tk.END)
            self.code_display.insert(tk.END, content)
            
    def save_file(self):
        # حفظ التعديلات
        if not self.current_file:
            messagebox.showerror("خطأ", "لم يتم تحميل أي ملف")
            return
            
        new_code = self.code_display.get(1.0, tk.END)
        save_path = filedialog.asksaveasfilename(
            initialfile=os.path.basename(self.current_file),
            defaultextension=".py"
        )
        
        if save_path:
            result = self.assistant.save_changes(new_code, save_path)
            messagebox.showinfo("تم الحفظ", result)
            
    def analyze_code(self):
        # طلب تحليل الكود
        if not self.current_file:
            messagebox.showerror("خطأ", "الرجاء تحميل ملف أولاً")
            return
            
        response = self.assistant.process_command("قم بتحليل الكود الحالي واقترح تحسينات")
        self.update_chat("المساعد", response)
        
    def process_assistant_response(self, message):
        # معالجة الرد من المساعد
        response = self.assistant.process_command(message)
        self.update_chat("المساعد", response)
        
    def clear_screen(self):
        # مسح الشاشة
        self.chat_history.configure(state='normal')
        self.chat_history.delete(1.0, tk.END)
        self.chat_history.configure(state='disabled')
        self.code_display.delete(1.0, tk.END)
        self.current_file = None
        
if __name__ == "__main__":
    API_KEY = "sk-6f828a6b26234bcfb2df6121fea97ef5"  # استبدل بمفتاحك
    
    root = tk.Tk()
    gui = CodeAssistantGUI(root, API_KEY)
    root.mainloop()