from django import forms
from django.core.exceptions import ValidationError, NON_FIELD_ERRORS


class TenantAdminBaseModelForm(forms.ModelForm):
    def add_error(self, field, error):
        """
        Override add_error to handle cases where 'company' is a readonly field
        and an error is attributed to it. Such errors are remapped to NON_FIELD_ERRORS.
        """
        # field can be None, which Django treats as NON_FIELD_ERRORS
        field_name = field if field is not None else NON_FIELD_ERRORS

        # Check if the error is for the 'company' field AND 'company' is not an editable field
        # in this form (i.e., it was made readonly by admin and thus not in self.fields).
        if field_name == 'company' and 'company' not in self.fields:
            # Remap to NON_FIELD_ERRORS. Prepend "Company:" to the message for clarity.
            remapped_errors = []
            current_error_messages = []

            if isinstance(error, ValidationError):
                current_error_messages = error.messages  # This is a list
            elif isinstance(error, (list, tuple)):
                current_error_messages = [str(e) for e in error]
            else:  # Assuming error is a string or single message
                current_error_messages = [str(error)]

            for msg in current_error_messages:
                remapped_errors.append(f"Company: {msg}")

            # Add remapped errors to NON_FIELD_ERRORS
            # super().add_error needs a field name (None for non-field) and an error (string, list, or ValidationError)
            super().add_error(None, ValidationError(remapped_errors, code=getattr(error, 'code',
                                                                                  None)))  # Pass code if ValidationError had one
        else:
            # Default behavior for other fields or if 'company' is a normal field
            super().add_error(field, error)