class EmailNormalizer
  def self.normalize(email)
    email.to_s.strip.downcase.squeeze(" ")
  end
end
